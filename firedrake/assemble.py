import numpy
import ufl
from collections import defaultdict
from itertools import chain

from pyop2 import op2
from pyop2.base import collecting_loops
from pyop2.exceptions import MapValueError, SparsityFormatError

from firedrake import assemble_expressions
from firedrake import tsfc_interface
from firedrake import function
from firedrake import matrix
from firedrake import parameters
from firedrake import solving
from firedrake import utils
from firedrake.slate import slate
from firedrake.slate import slac


__all__ = ["assemble"]


def assemble(f, tensor=None, bcs=None, form_compiler_parameters=None,
             inverse=False, mat_type=None, sub_mat_type=None,
             appctx={}, options_prefix=None, **kwargs):
    """Evaluate f.

    :arg f: a :class:`~ufl.classes.Form`, :class:`~ufl.classes.Expr` or
            a :class:`~slate.TensorBase` expression.
    :arg tensor: an existing tensor object to place the result in
         (optional).
    :arg bcs: a list of boundary conditions to apply (optional).
    :arg form_compiler_parameters: (optional) dict of parameters to pass to
         the form compiler.  Ignored if not assembling a
         :class:`~ufl.classes.Form`.  Any parameters provided here will be
         overridden by parameters set on the :class:`~ufl.classes.Measure` in the
         form.  For example, if a ``quadrature_degree`` of 4 is
         specified in this argument, but a degree of 3 is requested in
         the measure, the latter will be used.
    :arg inverse: (optional) if f is a 2-form, then assemble the inverse
         of the local matrices.
    :arg mat_type: (optional) string indicating how a 2-form (matrix) should be
         assembled -- either as a monolithic matrix ('aij' or 'baij'), a block matrix
         ('nest'), or left as a :class:`.ImplicitMatrix` giving matrix-free
         actions ('matfree').  If not supplied, the default value in
         ``parameters["default_matrix_type"]`` is used.  BAIJ differs
         from AIJ in that only the block sparsity rather than the dof
         sparsity is constructed.  This can result in some memory
         savings, but does not work with all PETSc preconditioners.
         BAIJ matrices only make sense for non-mixed matrices.
    :arg sub_mat_type: (optional) string indicating the matrix type to
         use *inside* a nested block matrix.  Only makes sense if
         ``mat_type`` is ``nest``.  May be one of 'aij' or 'baij'.  If
         not supplied, defaults to ``parameters["default_sub_matrix_type"]``.
    :arg appctx: Additional information to hang on the assembled
         matrix if an implicit matrix is requested (mat_type "matfree").
    :arg options_prefix: PETSc options prefix to apply to matrices.

    If f is a :class:`~ufl.classes.Form` then this evaluates the corresponding
    integral(s) and returns a :class:`float` for 0-forms, a
    :class:`.Function` for 1-forms and a :class:`.Matrix` or :class:`.ImplicitMatrix`
    for 2-forms.

    If f is an expression other than a form, it will be evaluated
    pointwise on the :class:`.Function`\s in the expression. This will
    only succeed if all the Functions are on the same
    :class:`.FunctionSpace`.

    If f is a Slate tensor expression, then it will be compiled using Slate's
    linear algebra compiler.

    If ``tensor`` is supplied, the assembled result will be placed
    there, otherwise a new object of the appropriate type will be
    returned.

    If ``bcs`` is supplied and ``f`` is a 2-form, the rows and columns
    of the resulting :class:`.Matrix` corresponding to boundary nodes
    will be set to 0 and the diagonal entries to 1. If ``f`` is a
    1-form, the vector entries at boundary nodes are set to the
    boundary condition values.
    """

    if "nest" in kwargs:
        nest = kwargs.pop("nest")
        from firedrake.logging import warning, RED
        warning(RED % "The 'nest' argument is deprecated, please set 'mat_type' instead")
        if nest is not None:
            mat_type = "nest" if nest else "aij"

    collect_loops = kwargs.pop("collect_loops", False)
    allocate_only = kwargs.pop("allocate_only", False)
    if len(kwargs) > 0:
        raise TypeError("Unknown keyword arguments '%s'" % ', '.join(kwargs.keys()))

    if isinstance(f, (ufl.form.Form, slate.TensorBase)):
        return _assemble(f, tensor=tensor, bcs=solving._extract_bcs(bcs),
                         form_compiler_parameters=form_compiler_parameters,
                         inverse=inverse, mat_type=mat_type,
                         sub_mat_type=sub_mat_type, appctx=appctx,
                         collect_loops=collect_loops,
                         allocate_only=allocate_only,
                         options_prefix=options_prefix)
    elif isinstance(f, ufl.core.expr.Expr):
        return assemble_expressions.assemble_expression(f)
    else:
        raise TypeError("Unable to assemble: %r" % f)


def allocate_matrix(f, bcs=None, form_compiler_parameters=None,
                    inverse=False, mat_type=None, sub_mat_type=None, appctx={},
                    options_prefix=None):
    """Allocate a matrix given a form.  To be used with :func:`create_assembly_callable`.

    .. warning::

       Do not use this function unless you know what you're doing.
    """
    return _assemble(f, bcs=bcs, form_compiler_parameters=form_compiler_parameters,
                     inverse=inverse, mat_type=mat_type, sub_mat_type=sub_mat_type,
                     appctx=appctx, allocate_only=True,
                     options_prefix=options_prefix)


def create_assembly_callable(f, tensor=None, bcs=None, form_compiler_parameters=None,
                             inverse=False, mat_type=None, sub_mat_type=None):
    """Create a callable object than be used to assemble f into a tensor.

    This is really only designed to be used inside residual and
    jacobian callbacks, since it always assembles back into the
    initially provided tensor.  See also :func:`allocate_matrix`.

    .. warning::

       Really do not use this function unless you know what you're doing.
    """
    if tensor is None:
        raise ValueError("Have to provide tensor to write to")
    if mat_type == "matfree":
        return tensor.assemble
    loops = _assemble(f, tensor=tensor, bcs=bcs,
                      form_compiler_parameters=form_compiler_parameters,
                      inverse=inverse, mat_type=mat_type,
                      sub_mat_type=sub_mat_type,
                      collect_loops=True)

    def thunk():
        for kernel in loops:
            kernel()
    return thunk


@utils.known_pyop2_safe
def _assemble(f, tensor=None, bcs=None, form_compiler_parameters=None,
              inverse=False, mat_type=None, sub_mat_type=None,
              appctx={},
              options_prefix=None,
              collect_loops=False,
              allocate_only=False):
    """Assemble the form or Slate expression f and return a Firedrake object
    representing the result. This will be a :class:`float` for 0-forms/rank-0
    Slate tensors, a :class:`.Function` for 1-forms/rank-1 Slate tensors and
    a :class:`.Matrix` for 2-forms/rank-2 Slate tensors.

    :arg bcs: A tuple of :class`.DirichletBC`\s to be applied.
    :arg tensor: An existing tensor object into which the form should be
        assembled. If this is not supplied, a new tensor will be created for
        the purpose.
    :arg form_compiler_parameters: (optional) dict of parameters to pass to
        the form compiler.
    :arg inverse: (optional) if f is a 2-form, then assemble the inverse
         of the local matrices.
    :arg mat_type: (optional) type for assembled matrices, one of
        "nest", "aij", "baij", or "matfree".
    :arg sub_mat_type: (optional) type for assembled sub matrices
        inside a "nest" matrix.  One of "aij" or "baij".
    :arg appctx: Additional information to hang on the assembled
         matrix if an implicit matrix is requested (mat_type "matfree").
    :arg options_prefix: An options prefix for the PETSc matrix
        (ignored if not assembling a bilinear form).
    """
    if mat_type is None:
        mat_type = parameters.parameters["default_matrix_type"]
    if mat_type not in ["matfree", "aij", "baij", "nest"]:
        raise ValueError("Unrecognised matrix type, '%s'" % mat_type)
    if sub_mat_type is None:
        sub_mat_type = parameters.parameters["default_sub_matrix_type"]
    if sub_mat_type not in ["aij", "baij"]:
        raise ValueError("Invalid submatrix type, '%s' (not 'aij' or 'baij')", sub_mat_type)

    if form_compiler_parameters:
        form_compiler_parameters = form_compiler_parameters.copy()
    else:
        form_compiler_parameters = {}
    form_compiler_parameters["assemble_inverse"] = inverse

    topology = f.ufl_domains()[0].topology
    for m in f.ufl_domains():
        # Ensure mesh is "initialised" (could have got here without
        # building a functionspace (e.g. if integrating a constant)).
        m.init()
        if m.topology != topology:
            raise NotImplementedError("All integration domains must share a mesh topology.")

    for o in chain(f.arguments(), f.coefficients()):
        domain = o.ufl_domain()
        if domain is not None and domain.topology != topology:
            raise NotImplementedError("Assembly with multiple meshes not supported.")

    if isinstance(f, slate.TensorBase):
        kernels = slac.compile_expression(f, tsfc_parameters=form_compiler_parameters)
        integral_types = [kernel.kinfo.integral_type for kernel in kernels]
    else:
        kernels = tsfc_interface.compile_form(f, "form", parameters=form_compiler_parameters, inverse=inverse)
        integral_types = [integral.integral_type() for integral in f.integrals()]

    rank = len(f.arguments())

    is_mat = rank == 2
    is_vec = rank == 1

    if any((coeff.function_space() and coeff.function_space().component is not None)
           for coeff in f.coefficients()):
        raise NotImplementedError("Integration of subscripted VFS not yet implemented")

    if inverse and rank != 2:
        raise ValueError("Can only assemble the inverse of a 2-form")

    zero_tensor = lambda: None

    if is_mat:
        matfree = mat_type == "matfree"
        nest = mat_type == "nest"
        if nest:
            baij = sub_mat_type == "baij"
        else:
            baij = mat_type == "baij"
        if matfree:  # intercept matrix-free matrices here
            if inverse:
                raise NotImplementedError("Inverse not implemented with matfree")
            if collect_loops:
                raise NotImplementedError("Can't collect loops with matfree")
            if tensor is None:
                return matrix.ImplicitMatrix(f, bcs,
                                             fc_params=form_compiler_parameters,
                                             appctx=appctx,
                                             options_prefix=options_prefix)
            if not isinstance(tensor, matrix.ImplicitMatrix):
                raise ValueError("Expecting implicit matrix with matfree")
            tensor.assemble()
            return tensor
        test, trial = f.arguments()

        map_pairs = []
        cell_domains = []
        exterior_facet_domains = []
        interior_facet_domains = []
        if tensor is None:
            # For horizontal facets of extruded meshes, the corresponding domain
            # in the base mesh is the cell domain. Hence all the maps used for top
            # bottom and interior horizontal facets will use the cell to dofs map
            # coming from the base mesh as a starting point for the actual dynamic map
            # computation.
            for integral_type in integral_types:
                if integral_type == "cell":
                    cell_domains.append(op2.ALL)
                elif integral_type == "exterior_facet":
                    exterior_facet_domains.append(op2.ALL)
                elif integral_type == "interior_facet":
                    interior_facet_domains.append(op2.ALL)
                elif integral_type == "exterior_facet_bottom":
                    cell_domains.append(op2.ON_BOTTOM)
                elif integral_type == "exterior_facet_top":
                    cell_domains.append(op2.ON_TOP)
                elif integral_type == "exterior_facet_vert":
                    exterior_facet_domains.append(op2.ALL)
                elif integral_type == "interior_facet_horiz":
                    cell_domains.append(op2.ON_INTERIOR_FACETS)
                elif integral_type == "interior_facet_vert":
                    interior_facet_domains.append(op2.ALL)
                else:
                    raise ValueError('Unknown integral type "%s"' % integral_type)

            # To avoid an extra check for extruded domains, the maps that are being passed in
            # are DecoratedMaps. For the non-extruded case the DecoratedMaps don't restrict the
            # space over which we iterate as the domains are dropped at Sparsity construction
            # time. In the extruded case the cell domains are used to identify the regions of the
            # mesh which require allocation in the sparsity.
            if cell_domains:
                map_pairs.append((op2.DecoratedMap(test.cell_node_map(), cell_domains),
                                  op2.DecoratedMap(trial.cell_node_map(), cell_domains)))
            if exterior_facet_domains:
                map_pairs.append((op2.DecoratedMap(test.exterior_facet_node_map(), exterior_facet_domains),
                                  op2.DecoratedMap(trial.exterior_facet_node_map(), exterior_facet_domains)))
            if interior_facet_domains:
                map_pairs.append((op2.DecoratedMap(test.interior_facet_node_map(), interior_facet_domains),
                                  op2.DecoratedMap(trial.interior_facet_node_map(), interior_facet_domains)))

            map_pairs = tuple(map_pairs)
            # Construct OP2 Mat to assemble into
            fs_names = (test.function_space().name, trial.function_space().name)

            try:
                sparsity = op2.Sparsity((test.function_space().dof_dset,
                                         trial.function_space().dof_dset),
                                        map_pairs,
                                        "%s_%s_sparsity" % fs_names,
                                        nest=nest,
                                        block_sparse=baij)
            except SparsityFormatError:
                raise ValueError("Monolithic matrix assembly is not supported for systems with R-space blocks.")

            result_matrix = matrix.Matrix(f, bcs, sparsity, numpy.float64,
                                          "%s_%s_matrix" % fs_names,
                                          options_prefix=options_prefix)
            tensor = result_matrix._M
        else:
            if isinstance(tensor, matrix.ImplicitMatrix):
                raise ValueError("Expecting matfree with implicit matrix")

            result_matrix = tensor
            # Replace any bcs on the tensor we passed in
            result_matrix.bcs = bcs
            tensor = tensor._M
            zero_tensor = tensor.zero

        if result_matrix.block_shape != (1, 1) and mat_type == "baij":
            raise ValueError("BAIJ matrix type makes no sense for mixed spaces, use 'aij'")

        def mat(testmap, trialmap, i, j):
            m = testmap(test.function_space()[i])
            n = trialmap(trial.function_space()[j])
            maps = (m[op2.i[0]] if m else None,
                    n[op2.i[1 if m else 0]] if n else None)
            return tensor[i, j](op2.INC, maps)
        result = lambda: result_matrix
        if allocate_only:
            result_matrix._assembly_callback = None
            return result_matrix
    elif is_vec:
        test = f.arguments()[0]
        if tensor is None:
            result_function = function.Function(test.function_space())
            tensor = result_function.dat
        else:
            result_function = tensor
            tensor = result_function.dat
            zero_tensor = tensor.zero

        def vec(testmap, i):
            _testmap = testmap(test.function_space()[i])
            return tensor[i](op2.INC, _testmap[op2.i[0]] if _testmap else None)
        result = lambda: result_function
    else:
        # 0-forms are always scalar
        if tensor is None:
            tensor = op2.Global(1, [0.0])
        else:
            raise ValueError("Can't assemble 0-form into existing tensor")
        result = lambda: tensor.data[0]

    coefficients = f.coefficients()
    domains = f.ufl_domains()

    # These will be used to correctly interpret the "otherwise"
    # subdomain
    all_integer_subdomain_ids = defaultdict(list)
    for k in kernels:
        if k.kinfo.subdomain_id != "otherwise":
            all_integer_subdomain_ids[k.kinfo.integral_type].append(k.kinfo.subdomain_id)
    for k, v in all_integer_subdomain_ids.items():
        all_integer_subdomain_ids[k] = tuple(sorted(v))

    # Since applying boundary conditions to a matrix changes the
    # initial assembly, to support:
    #     A = assemble(a)
    #     bc.apply(A)
    #     solve(A, ...)
    # we need to defer actually assembling the matrix until just
    # before we need it (when we know if there are any bcs to be
    # applied).  To do so, we build a closure that carries out the
    # assembly and stash that on the Matrix object.  When we hit a
    # solve, we funcall the closure with any bcs the Matrix now has to
    # assemble it.

    # In collecting loops mode, we collect the loops, and assume the
    # boundary conditions provided are the ones we want.  It therefore
    # is only used inside residual and jacobian assembly.
    loops = []

    def thunk(bcs):
        if collect_loops:
            loops.append(zero_tensor)
        else:
            zero_tensor()
        for indices, (kernel, integral_type, needs_orientations, subdomain_id, domain_number, coeff_map, needs_cell_facets, pass_layer_arg) in kernels:
            m = domains[domain_number]
            subdomain_data = f.subdomain_data()[m]
            # Find argument space indices
            if is_mat:
                i, j = indices
            elif is_vec:
                i, = indices
            else:
                assert len(indices) == 0

            sdata = subdomain_data.get(integral_type, None)
            if integral_type != 'cell' and sdata is not None:
                raise NotImplementedError("subdomain_data only supported with cell integrals.")

            # Extract block from tensor and test/trial spaces
            # FIXME Ugly variable renaming required because functions are not
            # lexical closures in Python and we're writing to these variables
            if is_mat and result_matrix.block_shape > (1, 1):
                tsbc = []
                trbc = []
                # Unwind ComponentFunctionSpace to check for matching BCs
                for bc in bcs:
                    fs = bc.function_space()
                    if fs.component is not None:
                        fs = fs.parent
                    if fs.index == i:
                        tsbc.append(bc)
                    if fs.index == j:
                        trbc.append(bc)
            elif is_mat:
                tsbc, trbc = bcs, bcs

            # Now build arguments for the par_loop
            kwargs = {}
            # Some integrals require non-coefficient arguments at the
            # end (facet number information).
            extra_args = []
            # Decoration for applying to matrix maps in extruded case
            decoration = None
            itspace = m.measure_set(integral_type, subdomain_id,
                                    all_integer_subdomain_ids)
            if integral_type == "cell":
                itspace = sdata or itspace

                if subdomain_id not in ["otherwise", "everywhere"] and \
                   sdata is not None:
                    raise ValueError("Cannot use subdomain data and subdomain_id")

                def get_map(x, bcs=None, decoration=None):
                    return x.cell_node_map(bcs)

            elif integral_type in ("exterior_facet", "exterior_facet_vert"):
                extra_args.append(m.exterior_facets.local_facet_dat(op2.READ))

                def get_map(x, bcs=None, decoration=None):
                    return x.exterior_facet_node_map(bcs)

            elif integral_type in ("exterior_facet_top", "exterior_facet_bottom"):
                # In the case of extruded meshes with horizontal facet integrals, two
                # parallel loops will (potentially) get created and called based on the
                # domain id: interior horizontal, bottom or top.
                decoration = {"exterior_facet_top": op2.ON_TOP,
                              "exterior_facet_bottom": op2.ON_BOTTOM}[integral_type]
                kwargs["iterate"] = decoration

                def get_map(x, bcs=None, decoration=None):
                    map_ = x.cell_node_map(bcs)
                    if decoration is not None:
                        return op2.DecoratedMap(map_, decoration)
                    return map_

            elif integral_type in ("interior_facet", "interior_facet_vert"):
                extra_args.append(m.interior_facets.local_facet_dat(op2.READ))

                def get_map(x, bcs=None, decoration=None):
                    return x.interior_facet_node_map(bcs)

            elif integral_type == "interior_facet_horiz":
                decoration = op2.ON_INTERIOR_FACETS
                kwargs["iterate"] = decoration

                def get_map(x, bcs=None, decoration=None):
                    map_ = x.cell_node_map(bcs)
                    if decoration is not None:
                        return op2.DecoratedMap(map_, decoration)
                    return map_

            else:
                raise ValueError("Unknown integral type '%s'" % integral_type)

            # Output argument
            if is_mat:
                tensor_arg = mat(lambda s: get_map(s, tsbc, decoration),
                                 lambda s: get_map(s, trbc, decoration),
                                 i, j)
            elif is_vec:
                tensor_arg = vec(lambda s: get_map(s), i)
            else:
                tensor_arg = tensor(op2.INC)

            coords = m.coordinates
            args = [kernel, itspace, tensor_arg,
                    coords.dat(op2.READ, get_map(coords)[op2.i[0]])]
            if needs_orientations:
                o = m.cell_orientations()
                args.append(o.dat(op2.READ, get_map(o)[op2.i[0]]))
            for n in coeff_map:
                c = coefficients[n]
                for c_ in c.split():
                    m_ = get_map(c_)
                    args.append(c_.dat(op2.READ, m_ and m_[op2.i[0]]))
            if needs_cell_facets:
                assert integral_type == "cell"
                extra_args.append(m.cell_to_facets(op2.READ))

            args.extend(extra_args)
            kwargs["pass_layer_arg"] = pass_layer_arg

            try:
                with collecting_loops(collect_loops):
                    loops.append(op2.par_loop(*args, **kwargs))
            except MapValueError:
                raise RuntimeError("Integral measure does not match measure of all coefficients/arguments")

        # Must apply bcs outside loop over kernels because we may wish
        # to apply bcs to a block which is otherwise zero, and
        # therefore does not have an associated kernel.
        if bcs is not None and is_mat:
            for bc in bcs:
                fs = bc.function_space()
                # Evaluate this outwith a "collecting_loops" block,
                # since creation of the bc nodes actually can create a
                # par_loop.
                nodes = bc.nodes
                if len(fs) > 1:
                    raise RuntimeError("""Cannot apply boundary conditions to full mixed space. Did you forget to index it?""")
                shape = result_matrix.block_shape
                with collecting_loops(collect_loops):
                    for i in range(shape[0]):
                        for j in range(shape[1]):
                            # Set diagonal entries on bc nodes to 1 if the current
                            # block is on the matrix diagonal and its index matches the
                            # index of the function space the bc is defined on.
                            if i != j:
                                continue
                            if fs.component is None and fs.index is not None:
                                # Mixed, index (no ComponentFunctionSpace)
                                if fs.index == i:
                                    loops.append(tensor[i, j].set_local_diagonal_entries(nodes))
                            elif fs.component is not None:
                                # ComponentFunctionSpace, check parent index
                                if fs.parent.index is not None:
                                    # Mixed, index doesn't match
                                    if fs.parent.index != i:
                                        continue
                                # Index matches
                                loops.append(tensor[i, j].set_local_diagonal_entries(nodes, idx=fs.component))
                            elif fs.index is None:
                                loops.append(tensor[i, j].set_local_diagonal_entries(nodes))
                            else:
                                raise RuntimeError("Unhandled BC case")
        if bcs is not None and is_vec:
            if len(bcs) > 0 and collect_loops:
                raise NotImplementedError("Loop collection not handled in this case")
            for bc in bcs:
                bc.apply(result_function)
        if is_mat:
            # Queue up matrix assembly (after we've done all the other operations)
            loops.append(tensor.assemble())
        return result()

    if collect_loops:
        thunk(bcs)
        return loops
    if is_mat:
        result_matrix._assembly_callback = thunk
        return result()
    else:
        return thunk(bcs)
