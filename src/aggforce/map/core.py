r"""Provides objects describing molecular maps.

These maps transform fine-grained points to coarse-grained points. Points may be
positions or forces.
"""

from typing import Union, List, Callable, Final, Dict, Optional, Literal
import numpy as np
from ..util import trjdot


# this should be very fast
def _has_nans(x: np.ndarray) -> bool:
    flat = x.ravel(order="K")
    # this will be a 1 element array
    return bool(np.isnan(np.dot(flat, flat)))


# _Taggable is only used by CLAMap right now, but its a separate class
# to keep things clear.
class _Taggable:
    r"""Provides a basic interface for having tags.

    Map optimizations sometimes need ways to annotate objects beyond what is
    easily added as an attribute. Having a tags dictionary provides a place
    where an arbitrary key-value (string:string) store can be described.
    """

    def __init__(self, tags: Union[None, Dict[str, str]]) -> None:
        """Initialize map.

        Arguments:
        ---------
        tags (dict):
            Used to create self.tags, which is a dictionary that than be
            arbitrarily populated at initialization. Useful for optimization
            methods to pass logs and residuals when a proper interface does not
            exist.
        """
        if tags is None:
            tags = {}
        self.tags = tags
        return


class LinearMap:
    r"""Unified interface for linear maps.

    Allows multiple different representations of the same map to be used.
    Various representations are converted into a consistent internal
    representation, which is then transformed into other formats when needed.

    The primary map format is the "standard_matrix". Given that our linear map
    transforms configurations from the fine-grained (fg) resolution to the
    coarse-grained (cg) resolution, the standard matrix is a (num. of cg
    particles) x (num. of fg particles) where each element describes how a fg
    particle linearly contributes to a cg particle. This can be accessed using
    the standard_matrix attribute.

    Calling instances allows them to map trajectory arrays of the shape
    (n_steps,n_sites,n_dims).
    """

    n_dim: Final = 3

    def __init__(
        self,
        mapping: Union[List[List[int]], np.ndarray],
        n_fg_sites: Union[int, None] = None,
        handle_nans: Union[bool, Literal["safe"]] = True,
        nan_check_threshold: float = 1e-6,
    ) -> None:
        r"""Initialize LinearMapping from something describing a map.

        Arguments:
        ---------
        mapping (list of lists of integers or 2-d numpy.ndarray):
            If a list of lists, then we assume the outer list iterates over
            various cg indices, and the inner lists describe the indices of
            which atoms contribute to that particular cg site. As this format
            does not make it clear how many total fg sites there are, n_fg_sites
            must be specified. If instead a 2-d numpy.ndarray, then the array is
            assumed to be of shape (num of cg,num of fg), which each element
            describing the coefficient of how the fg site contributes to the cg
            site. In this case n_fg_sites should not be specified.
        n_fg_sites (integer or None):
            Certain mapping descriptions make it ambiguous how many total
            fine-grained sites there are. This variable allows this ambiguity to
            be resolved.
        handle_nans:
            If true, np.nans in the matrices given as input to class calls are treated
            in a special way:
                1) They are converted to np.inf
                2) If any inf values exist in the output, an exception is raised
                3) All nans in the output are set to 0
            0*inf = nan, but all other numbers satisfy c*inf=+-inf; as a result, this
            procedure allows input arrays that have nans to be operated on such that
            if 0 is multiplied with that entry, 0 is returned.
            If safe, this is also done, but we make sure that no temporary modifications
            are performed in the input matrix (else, we may temporarily in-place set
            Nan to Inf). If False, simple matrix multiplication is performed without
            NaN specific logic.
        nan_check_threshold:
            If handle_nans is True, this used with np.allclose to make sure the
            corresponding tests passes.

        Example:
        -------
            [[0,2,3],[4]] with n_fg_sites=6 describes a 6 particle fg system
            and 2 particle cg system (from the length of the outer list).
            cg particle 0 (equally) depends on fg particles 0,2, and 3 as where
            cg particle 1 depends only on fg particle 4.

            The same information is given by the following 2-d matrix:
                [ 1/3 0   1/3 1/3 0   0  ]
                [ 0   0   0   0   1   0  ]
            Note that in the matrix case, we had to specify the normalization of
            the weights directly, as where in the list format it was done
            automatically.

        """
        if isinstance(mapping, np.ndarray) and len(mapping.shape) == 2:
            if n_fg_sites is not None:
                raise ValueError(
                    "Cannot specify n_fg_sites when mapping is ArrayLike. "
                    "Let it be inferred."
                )
            self._standard_matrix = mapping
            if len(self.standard_matrix.shape) != 2:
                raise ValueError(
                    f"mapping ({mapping}) does not cast into a 2-d numpy array."
                )
        elif hasattr(mapping, "__iter__"):
            # assume we are in the case of iterable of lists
            if n_fg_sites is None:
                raise ValueError()
            mapping = list(mapping)
            n_cg_sites = len(mapping)
            mapping_mat = np.zeros((n_cg_sites, n_fg_sites))
            for site, site_contents in enumerate(mapping):
                local_map = np.zeros(n_fg_sites)
                local_map[site_contents] = 1 / len(site_contents)
                mapping_mat[site, :] = local_map
            self._standard_matrix = mapping_mat
        else:
            raise ValueError("Cannot understanding mapping f{mapping}.")

        self.handle_nans = handle_nans
        if self.handle_nans:
            if not np.all(np.isfinite(self.standard_matrix)):
                raise ValueError(
                    "Nan checking can only be performed in "
                    "standard_matrix is itself finite."
                )
        self.nan_check_threshold = nan_check_threshold

    @property
    def standard_matrix(self) -> np.ndarray:
        r"""The mapping in standard matrix format."""
        return self._standard_matrix

    @property
    def n_cg_sites(self) -> int:
        r"""Number of coarse-grained sites in output of map."""
        return self.standard_matrix.shape[0]

    @property
    def n_fg_sites(self) -> int:
        r"""Number of fine-grained sites in input of map."""
        return self.standard_matrix.shape[1]

    @property
    def participating_fg(self) -> List[List[int]]:
        r"""Table of atoms included in cg site.

        Formatted as a dictionary of {cg_index:<list_of_fg_indices>}.
        """
        inc_matrix = self.standard_matrix > 0
        ind_form = list(zip(*np.nonzero(inc_matrix)))
        table: List[List[int]] = []
        for _ in range(self.n_cg_sites):
            table.append([])
        for cg_ind, fg_ind in ind_form:
            table[cg_ind].append(fg_ind)
        return table

    def close_to_identity(self, threshold: float = 1e-8) -> bool:
        r"""Check to see if internal matrix is close to the identity.

        Returns False is not square or if Frobenius norm of distance to identity is more
        than threshold.
        """
        internal_shape = self.standard_matrix.shape
        if internal_shape[0] != internal_shape[0]:
            return False
        ident_like = np.identity(internal_shape[0], dtype=self.standard_matrix.dtype)
        if ((ident_like - self.standard_matrix) ** 2).sum() ** (0.5) > threshold:
            return False
        return True

    def __call__(
        self,
        points: np.ndarray,
    ) -> np.ndarray:
        r"""Apply map to a particular form of 3-dim array.

        Arguments:
        ---------
        points (np.ndarray):
            Assumed to be 3 dimensional of shape (n_steps,n_sites,n_dims). Note that
            if self.handle_nans is True, this array may be temporarily altered if
            it contains NaN values.

        Returns:
        -------
        Combines points along the n_sites dimension according to the internal
        map. Note that the handling of NaNs depends on initialization options.
        """
        nan_handling = self.handle_nans and _has_nans(points)
        if nan_handling:
            input_mask = np.isnan(points)
            if self.handle_nans == "safe":
                input_matrix = points.copy()
            else:
                input_matrix = points
            input_matrix[input_mask] = 0.0
            raw_result = trjdot(input_matrix, self.standard_matrix)
            input_matrix[input_mask] = -1.0
            pushed_result = trjdot(input_matrix, self.standard_matrix)
            if not np.allclose(
                raw_result, pushed_result, atol=self.nan_check_threshold
            ):
                raise ValueError(
                    "NaN handling is on and results seem to depend on NaN "
                    "positions in input array. Check input and standard_matrix."
                )
            input_matrix[input_mask] = np.nan
            return raw_result
        else:
            return trjdot(points, self.standard_matrix)

    def flat_call(self, flattened: np.ndarray) -> np.ndarray:
        """Apply map to pre-flattened array.

        Array is reshaped, mapped, and then reshaped.

        Arguments:
        ---------
        flattened:
            2-D array of shape (n_frames,n_fg_sites*n_dim). Likely created by
            flattening a matrix of shape (n_frames,n_fg_sites*n_dim).

        Returns:
        -------
        Returns mapped array of shape (n_frames,n_cg_sites*n_dim).

        """
        shape = flattened.shape
        if len(shape) == 3:
            raise ValueError(f"Expected array of rank 2; got array with shape {shape}.")
        if flattened.shape[1] % self.n_dim != 0:
            raise ValueError(
                f"Array of shape {shape} can't be reshaped with dim of f{self.n_dim}."
            )
        reshaped = flattened.reshape(
            (flattened.shape[0], flattened.shape[1] // self.n_dim, self.n_dim),
        )
        transformed = self(reshaped)
        return transformed.reshape(
            (transformed.shape[0], transformed.shape[1] * transformed.shape[2]),
        )

    @property
    def T(self) -> "LinearMap":
        """LinearMap defined by transpose of its standard matrix."""
        return LinearMap(
            mapping=self.standard_matrix.T,
            handle_nans=self.handle_nans,
            nan_check_threshold=self.nan_check_threshold,
        )

    def __matmul__(self, lm: "LinearMap", /) -> "LinearMap":
        """LinearMap defined by multiplying the standard_matrix's of arguments."""
        return LinearMap(
            mapping=self.standard_matrix @ lm.standard_matrix,
            handle_nans=self.handle_nans,
            nan_check_threshold=self.nan_check_threshold,
        )

    def __rmul__(self, c: float, /) -> "LinearMap":
        """LinearMap defined by multiplying the standard_matrix's with a coefficient."""
        return LinearMap(
            mapping=c * self.standard_matrix,
            handle_nans=self.handle_nans,
            nan_check_threshold=self.nan_check_threshold,
        )

    def __add__(self, lm: "LinearMap", /) -> "LinearMap":
        """LinearMap defined by adding standard_matrices."""
        return LinearMap(
            mapping=self.standard_matrix + lm.standard_matrix,
            handle_nans=self.handle_nans,
            nan_check_threshold=self.nan_check_threshold,
        )

    def astype(self, *args, **kwargs) -> "LinearMap":
        """Convert to a given precision as determined by arguments.

        standard_matrix is converted to the stated dtype in the returned
        instance.  Arguments are passed to np astype. Setting copy to False may
        reduce copies, but may return instances with shared references.
        """
        return self.__class__(
            mapping=self.standard_matrix.astype(*args, **kwargs),
            handle_nans=self.handle_nans,
            nan_check_threshold=self.nan_check_threshold,
        )


class CLAMap(_Taggable):
    r"""Provide representation of a Co-Local Affine map.

    An affine transformation on x is Ax+b (a translation and linear
    transformation). Consider two time series, x_i and y_i. Consider mapping x_i
    using information from y_i to create a specific affine transformation for
    every x_i as so:
        Input:
            x_1, x_2, ..., x_T
            (with y_1, y_2, ..., y_T)
        Output:
            A(y_1)x_1+b(y1), A(y_2)x_2+b(y_2), ..., A(y_T)x_T+b(y_T)

    This is the type of map this object describes. It is specified by providing
    A(.) and b(.). A is referred to as scale, and b is referred to as trans.  We
    refer to the values being mapped as points, and the values creating A and b
    copoints (hence the name co-local affine map).

    This is a particular form of nonlinear map and is the output of featurized
    force maps. Unfortunately, it cannot be expressed using a single matrix
    (unlike linear maps), and so does not have a standard_matrix property.
    """

    n_dim: Final = 3

    def __init__(
        self,
        scale: Callable,
        trans: Callable,
        n_fg_sites: int,
        n_cg_sites: Optional[int] = None,
        zeroes_check: bool = True,
        tags: Optional[Dict[str, str]] = None,
    ) -> None:
        r"""Initialize CLAMap object from functions scale (A) and trans (b).

        Arguments:
        ---------
        scale (callable):
            Callable which accepts an array of shape (n_steps,n_fg_sites,n_dim)
            and returns (n_steps,n_fg_sites,n_cg_sites). See class description for
            more details.
        trans (callable):
            Callable which accepts an array of shape (n_steps,n_fg_sites,n_dim)
            and returns (n_steps,n_cg_sites,n_dim). See class description for
            more details.
        n_fg_sites (integer):
            Number of fg sites in the input space.
        n_cg_sites (integer or None):
            Number of cg sites in the output space. If set to None, then
            zero_check must be True, and it's value is inferred during the zero
            zero check.
        zeroes_check (boolean):
            If true, A and b are tested by creating a zeros array for both the
            points and copoints. The dimension of these arrays corresponds to a
            single frame trajectory and uses the n_fg_sites argument and n_dim
            class variable.
        tags (dictionary or None):
            Passed to Map init.
        """
        super().__init__(tags=tags)
        if zeroes_check:
            z_points = np.zeros((1, n_fg_sites, self.n_dim))
            mapped = trjdot(z_points, scale(z_points)) + trans(z_points)
            if n_cg_sites is None:
                n_cg_sites = mapped.shape[1]
            else:
                if n_cg_sites != mapped.shape[1]:
                    raise ValueError("n_cg_sites did not match results from zero test")
        else:
            if n_cg_sites is None:
                raise ValueError(
                    "If n_cg_sites is not set, zeroes_check must be truthy."
                )

        self._n_cg_sites: Final = n_cg_sites
        self._n_fg_sites: Final = n_fg_sites
        self.scale: Final = scale
        self.trans: Final = trans

    @property
    def n_cg_sites(self) -> int:
        r"""Number of coarse-grained sites in map output."""
        return self._n_cg_sites

    @property
    def n_fg_sites(self) -> int:
        r"""Number of fine-grained sites in map input."""
        return self._n_fg_sites

    def __call__(
        self, points: np.ndarray, copoints: Union[None, np.ndarray]
    ) -> np.ndarray:
        r"""Apply map to 3-dim array.

        Arguments:
        ---------
        points (np.ndarray):
            Assumed to be 3 dimensional of shape (n_steps,n_sites,n_dims).
        copoints (np.ndarray):
            Provided as input to A and b during mapping. Assume dot be of form
            (n_steps,n_sites,n_dims). See class description.

        Returns:
        -------
        Combines points along the n_sites dimension according to the internal
        map functions.
        """
        scale = self.scale(copoints)
        trans = self.trans(copoints)
        return trjdot(points, scale) + trans
