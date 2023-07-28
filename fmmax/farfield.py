"""Defines functions related to farfield patterns in the FMM scheme.

These functions are intended for calculations that involve Brillouin
zone integration, and batched in-plane wavevectors as generated by
`basis.brillouin_zone_in_plane_wavevector`.

Copyright (c) Meta Platforms, Inc. and affiliates.
"""

from typing import Callable, Tuple

import jax
import jax.numpy as jnp
import numpy as onp

from fmmax import basis, utils


def farfield_profile(
    flux: jnp.ndarray,
    wavelength: jnp.ndarray,
    in_plane_wavevector: jnp.ndarray,
    primitive_lattice_vectors: basis.LatticeVectors,
    expansion: basis.Expansion,
    brillouin_grid_axes: Tuple[int, int],
) -> Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Computes a farfield profile.

    This function effectively "unstacks" the values for each Fourier order and
    for each point in the Brillouin zone sampling scheme.

    Args:
        flux: The flux array, with shape `(..., num_bz_kx, num_bz_ky, ...
            2 * num_terms, num_sources)`.
        wavelength: The wavelength, batch-compatible with `flux`.
        in_plane_wavevector: The in-plane wavevector for the zeroth Fourier
            order, batch-compatible with `flux`.
        primitive_lattice_vectors: The primitive lattice vectors of the unit cell.
        expansion: The expansion used for the fields.
        brillouin_grid_axes: Specifies the two axes of `flux` corresponding to
            the Brillouin zone grid.

    Returns:
        The polar and azimuthal angles, solid angle associated with each value,
        and the farfield power.
    """
    assert flux.shape[-2] == 2 * expansion.num_terms
    brillouin_grid_axes: Tuple[int, int] = utils.absolute_axes(brillouin_grid_axes, flux.ndim)  # type: ignore[no-redef]

    ndim_batch = flux.ndim - 2
    wavelength = utils.atleast_nd(wavelength, ndim_batch)
    in_plane_wavevector = utils.atleast_nd(in_plane_wavevector, ndim_batch + 1)

    transverse_wavevectors = basis.transverse_wavevectors(
        in_plane_wavevector,
        primitive_lattice_vectors=primitive_lattice_vectors,
        expansion=expansion,
    )
    transverse_wavevectors = unflatten_transverse_wavevectors(
        transverse_wavevectors, expansion, brillouin_grid_axes
    )
    flux = unflatten_flux(flux, expansion, brillouin_grid_axes)

    # Remove the brillouin zone axes from `wavelength`, making it compatible
    # with the unflattened flux and transverse wavevectors.
    wavelength = jnp.squeeze(wavelength, axis=brillouin_grid_axes)

    polar_angle, azimuthal_angle = angles_from_unflattened_transverse_wavevectors(
        transverse_wavevectors=transverse_wavevectors,
        wavelength=wavelength,
    )

    # Transform flux form units of power per unit Brillouin zone area to
    # power per unit solid angle. Add dummy dimensions for the polarization
    # and sources.
    solid_angle = solid_angle_from_unflattened_transverse_wavevectors(
        transverse_wavevectors=transverse_wavevectors,
        wavelength=wavelength,
    )
    transformed_flux = flux / solid_angle[..., jnp.newaxis, jnp.newaxis]
    return polar_angle, azimuthal_angle, solid_angle, transformed_flux


def angles_from_unflattened_transverse_wavevectors(
    transverse_wavevectors: jnp.ndarray,
    wavelength: jnp.ndarray,
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """Computes the propagation angles in free space for given wavevectors.

    Evanescent modes whose transverse wavevector magnitude exceeds that of
    the free space wavevector are given a polar angle of `pi / 2`.

    Args:
        transverse_wavevectors: The unflattened transverse wavectors, with
            shape `(..., nkx, nky, 2)`.
        wavelength: The free-space wavelength.

    Returns:
        Arrays containing the polar and azimuthal angles.
    """
    assert transverse_wavevectors.ndim - 3 == wavelength.ndim
    kx = transverse_wavevectors[..., 0]
    ky = transverse_wavevectors[..., 1]
    kt = jnp.sqrt(kx**2 + ky**2)

    sin_polar_angle = kt * wavelength[..., jnp.newaxis, jnp.newaxis] / (2 * jnp.pi)
    polar_angle = jnp.where(
        sin_polar_angle > 1, jnp.pi / 2, jnp.arcsin(sin_polar_angle)
    )

    azimuthal_angle = jnp.angle(kx + 1j * ky)
    return polar_angle, azimuthal_angle


def solid_angle_from_unflattened_transverse_wavevectors(
    transverse_wavevectors: jnp.ndarray,
    wavelength: jnp.ndarray,
) -> jnp.ndarray:
    """Computes the solid angle associated with each transverse wavevector.

    The transverse wavevectors should be unflattened, i.e. the `-3` and `-2`
    axes should correspond to different points in k-space.

    Args:
        transverse_wavevectors: The unflattened transverse wavevectors
        wavelength: The free-space wavelength.

    Returns:
        The solid angle, with the shape matching the leading dimensions of
        `transverse_wavevectors`.
    """
    assert transverse_wavevectors.ndim >= 3

    kx = transverse_wavevectors[..., 0]
    ky = transverse_wavevectors[..., 1]

    # Normalize the wavevectors so they lie on the unit sphere.
    kx /= 2 * jnp.pi / wavelength[..., jnp.newaxis, jnp.newaxis]
    ky /= 2 * jnp.pi / wavelength[..., jnp.newaxis, jnp.newaxis]
    polar_angle = jnp.arcsin(jnp.sqrt(kx**2 + ky**2))

    # Each of our transverse wavevectors lies within a "cell" in the kxky plane.
    # Compute the area of each cell, and then project it onto the unit sphere
    # to get the solid angle associated with each transverse wavevector.
    #
    # First, compute the locations of the cell verteces.
    pad_width = ((0, 0),) * (kx.ndim - 2) + ((1, 1), (1, 1))
    vertex_kx = jnp.pad(kx, pad_width, mode="edge")
    vertex_ky = jnp.pad(ky, pad_width, mode="edge")
    vertex_kx = (
        vertex_kx[..., :-1, :-1]
        + vertex_kx[..., :-1, 1:]
        + vertex_kx[..., 1:, :-1]
        + vertex_kx[..., 1:, 1:]
    ) / 4
    vertex_ky = (
        vertex_ky[..., :-1, :-1]
        + vertex_ky[..., :-1, 1:]
        + vertex_ky[..., 1:, :-1]
        + vertex_ky[..., 1:, 1:]
    ) / 4
    vertex_kt = jnp.stack([vertex_kx, vertex_ky], axis=-1)

    # Find the vectors defining each parallelogramic cell.
    v1 = vertex_kt[..., :-1, 1:, :] - vertex_kt[..., :-1, :-1, :]
    v2 = vertex_kt[..., 1:, :-1, :] - vertex_kt[..., :-1, :-1, :]

    # Find the area of each parallelogramic cell.
    cell_area = jnp.abs(v1[..., 0] * v2[..., 1] - v2[..., 0] * v1[..., 1])

    # Project the area onto the unit sphere.
    projected_area = cell_area / jnp.cos(polar_angle)
    return projected_area


# -----------------------------------------------------------------------------
# Functions for computing the total flux in some angular cone.
# -----------------------------------------------------------------------------


def integrated_flux(
    flux: jnp.ndarray,
    wavelength: jnp.ndarray,
    in_plane_wavevector: jnp.ndarray,
    primitive_lattice_vectors: basis.LatticeVectors,
    expansion: basis.Expansion,
    brillouin_grid_axes: Tuple[int, int],
    angle_bounds_fn: Callable[[jnp.ndarray, jnp.ndarray], jnp.ndarray],
    upsample_factor: int,
) -> jnp.ndarray:
    """Computes the flux within the bounds defined by `angle_bounds_fn`.

    Args:
        flux: The flux array, with shape `(..., num_bz_kx, num_bz_ky, ...
            2 * num_terms, num_sources)`.
        wavelength: The wavelength, batch-compatible with `flux`.
        in_plane_wavevector: The in-plane wavevector for the zeroth Fourier
            order, batch-compatible with `flux`.
        primitive_lattice_vectors: The primitive lattice vectors of the unit cell.
        expansion: The expansion used for the fields.
        brillouin_grid_axes: Specifies the two axes of `flux` corresponding to
            the Brillouin zone grid.
        angle_bounds_fn: A function with signature `fn(polar_angle, azimuthal_angle)`
            returning a mask that is `True` for angles that should be included in
            the integral.
        upsample_factor: Integer factor specifying upsampling performed in the
            integral, which is used to approximate trapezoidal rule integration.

    Returns:
        The integrated flux, with shape equal to the batch dimensions of flux,
        excluding those for the brillouin zone grid.
    """
    brillouin_grid_axes: Tuple[int, int] = utils.absolute_axes(brillouin_grid_axes, flux.ndim)  # type: ignore[no-redef]

    # Compute the weights array, which reduce the integration weights to
    # an inner product.
    weights = _integrated_flux_weights(
        flux=flux,
        wavelength=wavelength,
        in_plane_wavevector=in_plane_wavevector,
        primitive_lattice_vectors=primitive_lattice_vectors,
        expansion=expansion,
        brillouin_grid_axes=brillouin_grid_axes,
        angle_bounds_fn=angle_bounds_fn,
        upsample_factor=upsample_factor,
    )

    # Sum over the Brillouin zone and Fourier order axes.
    return jnp.sum(weights * flux, axis=brillouin_grid_axes + (-2,))


def _integrated_flux_weights(
    flux: jnp.ndarray,
    wavelength: jnp.ndarray,
    in_plane_wavevector: jnp.ndarray,
    primitive_lattice_vectors: basis.LatticeVectors,
    expansion: basis.Expansion,
    brillouin_grid_axes: Tuple[int, int],
    angle_bounds_fn: Callable[[jnp.ndarray, jnp.ndarray], jnp.ndarray],
    upsample_factor: int,
) -> jnp.ndarray:
    """Returns the integration weights for the bounds defined by `angle_bounds_fn`."""
    brillouin_grid_axes: Tuple[int, int] = utils.absolute_axes(brillouin_grid_axes, flux.ndim)  # type: ignore[no-redef]

    def _integrated_fn(flux):
        assert flux.shape[-1] == 1
        return jnp.sum(
            _integrated_flux_upsampled(
                flux=flux,
                wavelength=wavelength,
                in_plane_wavevector=in_plane_wavevector,
                primitive_lattice_vectors=primitive_lattice_vectors,
                expansion=expansion,
                brillouin_grid_axes=brillouin_grid_axes,
                angle_bounds_fn=angle_bounds_fn,
                upsample_factor=upsample_factor,
            )
        )

    # The weights are just the gradient of the integrated flux with respect
    # to the flat array elements; use a dummy flux having a single source.
    dummy_flux = jnp.ones(flux.shape[:-1] + (1,))
    return jax.grad(_integrated_fn)(dummy_flux)


def _integrated_flux_upsampled(
    flux: jnp.ndarray,
    wavelength: jnp.ndarray,
    in_plane_wavevector: jnp.ndarray,
    primitive_lattice_vectors: basis.LatticeVectors,
    expansion: basis.Expansion,
    brillouin_grid_axes: Tuple[int, int],
    angle_bounds_fn: Callable[[jnp.ndarray, jnp.ndarray], jnp.ndarray],
    upsample_factor: int,
) -> jnp.ndarray:
    """Computes the flux within the bounds defined by `angle_bounds_fn`."""
    assert upsample_factor >= 1
    brillouin_grid_axes: Tuple[int, int] = utils.absolute_axes(brillouin_grid_axes, flux.ndim)  # type: ignore[no-redef]

    ndim_batch = flux.ndim - 2
    wavelength = utils.atleast_nd(wavelength, ndim_batch)
    in_plane_wavevector = utils.atleast_nd(in_plane_wavevector, ndim_batch + 1)

    transverse_wavevectors = basis.transverse_wavevectors(
        in_plane_wavevector=in_plane_wavevector,
        primitive_lattice_vectors=primitive_lattice_vectors,
        expansion=expansion,
    )

    flux = unflatten_flux(flux, expansion, brillouin_grid_axes)
    transverse_wavevectors = unflatten_transverse_wavevectors(
        transverse_wavevectors, expansion, brillouin_grid_axes
    )

    # Remove the `nan`s that are found at array locations having no associated
    # value in the original flattened arrays.
    flux = jnp.where(jnp.isnan(flux), 0, flux)
    transverse_wavevectors = jnp.where(
        jnp.isnan(transverse_wavevectors), 0, transverse_wavevectors
    )

    # Upsample the transverse wavevectors and flux to high resolution.
    assert flux.shape[-2] == 2
    upsampled_flux_shape = flux.shape[:-4] + (
        upsample_factor * flux.shape[-4],  # kx axis
        upsample_factor * flux.shape[-3],  # ky axis
        flux.shape[-2],  # Polarization axis, length 2
        flux.shape[-1],  # Source axis
    )
    flux = jax.image.resize(flux, upsampled_flux_shape, method="linear")

    assert transverse_wavevectors.shape[-1] == 2
    upsampled_wavevector_shape = transverse_wavevectors.shape[:-3] + (
        upsample_factor * transverse_wavevectors.shape[-3],  # kx axis
        upsample_factor * transverse_wavevectors.shape[-2],  # ky axis
        transverse_wavevectors.shape[-1],  # Direction axis.
    )
    transverse_wavevectors = jax.image.resize(
        transverse_wavevectors, upsampled_wavevector_shape, method="linear"
    )

    # Remove the brillouin grid axes from wavelength, and insert axes
    # for the unstacked wavevector.
    wavelength = jnp.squeeze(wavelength, brillouin_grid_axes)

    # Compute polar and azimuthal angles.
    polar_angle, azimuthal_angle = angles_from_unflattened_transverse_wavevectors(
        transverse_wavevectors, wavelength
    )

    selected = angle_bounds_fn(polar_angle, azimuthal_angle)
    selected = jnp.where(jnp.isnan(selected), False, selected)
    selected = selected[..., jnp.newaxis, jnp.newaxis]

    masked_flux = jnp.where(selected, flux, 0)

    # Sum over the kx, ky, and polarization axes.
    return jnp.sum(masked_flux, axis=(-4, -3, -2)) / upsample_factor**2


# -----------------------------------------------------------------------------
# Functions related to unflattening results of Brillouin zone integration.
# -----------------------------------------------------------------------------


def unflatten(flat: jnp.ndarray, expansion: basis.Expansion) -> jnp.ndarray:
    """Unflattens an array for a given expansion and Brillouin integration scheme.

    The returned array combines the values associated with all terms in the
    Fourier expansion at all points in the Brillouin zone grid in a single
    array with trailing axes havving shape `(num_kx, num_ky)`. Elements in the
    output which have no corresponding elements in `flat` are given a value
    of `nan`.

    The flat array should have shape `(..., num_bz_kx, num_bz_ky, num_terms)`,
    where `num_terms` is the number of terms in the Fourier expansion, and the
    `-3` and `-2` axes are for the Brillouin zone grid, as used e.g. with
    Brillouin zone integration to model localized sources.

    This function assumes that the Brillouin zone is sampled on a regular grid,
    as produced by `basis.brillouin_zone_in_plane_wavevector`.

    Args:
        flat: The flat array, with shape  `(..., num_bz_kx, num_bz_ky, num_terms)`.
        expansion: The expansion used for the array.

    Returns:
        The unflattened array, with shape `(batch_shape, num_kx, num_ky)`.
    """
    assert flat.ndim >= 3
    assert flat.shape[-1] == expansion.num_terms

    i = expansion.basis_coefficients[:, 0]
    j = expansion.basis_coefficients[:, 1]

    batch_shape = flat.shape[:-3]
    bz_grid_shape = flat.shape[-3:-1]

    # The shape of the output array shoudl accomodate all `(i, j)` values.
    shape = batch_shape + (
        (max(i) - min(i) + 1) * bz_grid_shape[0],
        (max(j) - min(j) + 1) * bz_grid_shape[1],
    )

    bz_i, bz_j = onp.meshgrid(
        onp.arange(bz_grid_shape[0]),
        onp.arange(bz_grid_shape[1]),
        indexing="ij",
    )

    offset_i = min(i) * bz_grid_shape[0]
    offset_j = min(j) * bz_grid_shape[1]

    merged_i = i * bz_grid_shape[0] + bz_i[..., onp.newaxis] - offset_i
    merged_j = j * bz_grid_shape[1] + bz_j[..., onp.newaxis] - offset_j

    stacked_i = merged_i.flatten()
    stacked_j = merged_j.flatten()
    stacked_flat = jnp.reshape(flat, batch_shape + (-1,))

    return jnp.full(shape, jnp.nan).at[..., stacked_i, stacked_j].set(stacked_flat)


def unflatten_flux(
    flux: jnp.ndarray,
    expansion: basis.Expansion,
    brillouin_grid_axes: Tuple[int, int],
) -> jnp.ndarray:
    """Unflattens a flux for a given expansion and Brillouin integration scheme.

    Args:
        flux: The flux array, with shape `(..., num_bz_kx, num_bz_ky, ...
            2 * num_terms, num_sources)`.
        expansion: The expansion used for the flux.
        brillouin_grid_axes: The axes associated with the Brillouin zone grid.

    Returns:
        The unflattened flux, with shape `(..., num_kx, num_ky, 2, num_sources)`.
    """
    assert flux.ndim >= 4
    assert flux.shape[-2] == 2 * expansion.num_terms
    brillouin_grid_axes: Tuple[int, int] = utils.absolute_axes(brillouin_grid_axes, flux.ndim)  # type: ignore[no-redef]

    # The flux array has values for two polarizations at each Fourier order. Split
    # these and treat them as a batch dimension.
    flux = jnp.reshape(flux, flux.shape[:-2] + (2, -1, flux.shape[-1]))

    # Transpose so the axes associated with the Fourier orders and Brillouin zone
    # grid are the trailing axes, as needed by `unflatten`.
    batch_axes = tuple(
        [i for i in range(flux.ndim) if i not in brillouin_grid_axes + (flux.ndim - 2,)]
    )
    axes = batch_axes + tuple([i for i in range(flux.ndim) if i not in batch_axes])
    flux = jnp.transpose(flux, axes)

    flux = unflatten(flux, expansion)

    # Transpose so that polarization and sources are returned to the trailing axes.
    axes = tuple(range(flux.ndim - 4)) + (-2, -1, -4, -3)
    return jnp.transpose(flux, axes)


def unflatten_transverse_wavevectors(
    transverse_wavevectors: jnp.ndarray,
    expansion: basis.Expansion,
    brillouin_grid_axes: Tuple[int, int],
) -> jnp.ndarray:
    """Unflattens transverse wavevectors for a given expansion and Brillouin integration scheme.

    Args:
        transverse_wavevectors: The transverse wavevectors array, with shape
            `(..., num_bz_kx, num_bz_ky, ..., num_terms, 2)`.
        expansion: The expansion used for the flux.
        brillouin_grid_axes: The axes associated with the Brillouin zone grid.

    Returns:
        The unflattened wavevectors, with shape `(..., num_kx, num_ky, 2)`.
    """
    assert transverse_wavevectors.ndim >= 4
    assert transverse_wavevectors.shape[-2:] == (expansion.num_terms, 2)
    brillouin_grid_axes: Tuple[int, int] = utils.absolute_axes(brillouin_grid_axes, transverse_wavevectors.ndim)  # type: ignore[no-redef]

    # Transpose so the axes associated with the Fourier orders and Brillouin zone
    # grid are the trailing axes, as needed by `unflatten`.
    non_batch_axes = brillouin_grid_axes + (transverse_wavevectors.ndim - 2,)
    batch_axes = tuple(
        [i for i in range(transverse_wavevectors.ndim) if i not in non_batch_axes]
    )
    axes = batch_axes + tuple(
        [i for i in range(transverse_wavevectors.ndim) if i not in batch_axes]
    )
    transverse_wavevectors = jnp.transpose(transverse_wavevectors, axes)

    transverse_wavevectors = unflatten(transverse_wavevectors, expansion)

    # Transpose so the trailing axis is for the wavevector direction.
    axes = tuple(range(transverse_wavevectors.ndim - 3)) + (-2, -1, -3)
    return jnp.transpose(transverse_wavevectors, axes)
