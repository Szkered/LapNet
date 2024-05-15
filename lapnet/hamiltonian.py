# Copyright 2020 DeepMind Technologies Limited.
# Copyright 2023 Bytedance Ltd. and/or its affiliate
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Evaluating the Hamiltonian on a wavefunction."""

from functools import partial
from typing import Any, Sequence

import chex
import jax
import jax.numpy as jnp
from jax import lax
from jax.experimental import jet
from typing_extensions import Protocol

from lapnet import networks


class LocalEnergy(Protocol):

  def __call__(
    self, params: networks.ParamTree, key: chex.PRNGKey, data: jnp.ndarray
  ) -> jnp.ndarray:
    """Returns the local energy of a Hamiltonian at a configuration.
    Args:
      params: network parameters.
      key: JAX PRNG state.
      data: MCMC configuration to evaluate.
    """


class MakeLocalEnergy(Protocol):

  def __call__(
    self,
    f: networks.WaveFuncLike,
    atoms: jnp.ndarray,
    charges: jnp.ndarray,
    nspins: Sequence[int],
    use_scan: bool = False,
    **kwargs: Any
  ) -> LocalEnergy:
    """Builds the LocalEnergy function.

    Args:
      f: Callable which evaluates the sign and log of the magnitude of the
        wavefunction.
      atoms: atomic positions.
      charges: nuclear charges.
      nspins: Number of particles of each spin.
      use_scan: Whether to use a `lax.scan` for computing the laplacian.
      **kwargs: additional kwargs to use for creating the specific Hamiltonian.
    """


def get_sdgd_idx_set(key, dim, n_sdgd_dim=16):
  if n_sdgd_dim != 0:
    idx_set = jax.random.choice(key, dim, shape=(n_sdgd_dim, ), replace=False)
  else:
    idx_set = jnp.arange(dim)
  return idx_set


def get_random_vec(
  key,
  idx_set,
  dim,
  n_sdgd_dim=8,
  method="sdgd",
  n_hte_vec=0,
):
  """Return a random vector on sdgd sampled dimensions, with Rademacher
  distribution.

  If with_time, add a one-hot time dimension at the end.
  """
  d = n_sdgd_dim or dim
  if method == "normal":  # HTE
    rand_vec = jax.random.normal(key, shape=(n_hte_vec, d))
  elif method == "unit":  # HTE
    rand_vec = 2 * (
      jax.random.randint(key, shape=(n_hte_vec, d), minval=0, maxval=2) - 0.5
    )
  elif method == "sdgd":
    rand_vec = jax.vmap(lambda i: jnp.eye(dim)[i])(idx_set) * dim
  else:
    raise ValueError
  return rand_vec
  # n_vec = n_hte_vec
  # d = dim
  # rand_sub_vec = jnp.zeros((n_vec, d)).at[:n_hte_vec, idx_set].set(rand_vec)
  # return rand_sub_vec


def local_kinetic_energy(
  f: networks.LogWaveFuncLike,
  use_scan: bool = False,
  forward_laplacian=True
) -> networks.LogWaveFuncLike:
  r"""Creates a function to for the local kinetic energy, -1/2 \nabla^2 ln|f|.

  Args:
    f: Callable which evaluates the log of the magnitude of the wavefunction.
    use_scan: Whether to use a `lax.scan` for computing the laplacian.
    partition_num: 0: fori_loop implementation
                   1: Hessian implementation
                   other positive integer: Split the laplacian to multiple trunks and
                                           calculate accordingly.

  Returns:
    Callable which evaluates the local kinetic energy,
    -1/2f \nabla^2 f = -1/2 (\nabla^2 log|f| + (\nabla log|f|)^2).
  """

  def _randomized_lapl_over_f(params, data, rng):
    dim = data.shape[0]
    idx_set = get_sdgd_idx_set(rng, dim)
    rand_sub_vec = get_random_vec(rng, idx_set, dim)

    f_partial = partial(f, params)
    taylor_2 = lambda v: jet.jet(
      fun=f_partial,
      primals=(data,),
      series=((v, jnp.zeros(dim)),),
    )
    _, (_, hvps) = jax.vmap(taylor_2)(rand_sub_vec)
    trace_est = jnp.mean(hvps)
    f_x = jax.grad(f_partial)(data)
    return -0.5 * (trace_est + jnp.sum(f_x**2))

  def _forward_lapl_over_f(params, data, rng):
    from lapjax import LapTuple, TupType
    output = f(params, LapTuple(data, is_input=True))
    return -0.5 * output.get(TupType.LAP) - \
            0.5 * jnp.sum(output.get(TupType.GRAD)**2)

  def _lapl_over_f(params, data, rng):
    n = data.shape[0]
    eye = jnp.eye(n)
    grad_f = jax.grad(f, argnums=1)
    grad_f_closure = lambda y: grad_f(params, y)
    primal, dgrad_f = jax.linearize(grad_f_closure, data)

    if use_scan:
      _, diagonal = lax.scan(
        lambda i, _: (i + 1, dgrad_f(eye[i])[i]), 0, None, length=n
      )
      result = -0.5 * jnp.sum(diagonal)
    else:
      result = -0.5 * lax.fori_loop(
        0, n, lambda i, val: val + dgrad_f(eye[i])[i], 0.0
      )
    return result - 0.5 * jnp.sum(primal**2)

  if forward_laplacian:
    return _forward_lapl_over_f
  else:
    return _randomized_lapl_over_f
    # return _lapl_over_f


def potential_electron_electron(r_ee: jnp.ndarray) -> jnp.ndarray:
  """Returns the electron-electron potential.

  Args:
    r_ee: Shape (neletrons, nelectrons, :). r_ee[i,j,0] gives the distance
      between electrons i and j. Other elements in the final axes are not
      required.
  """
  return jnp.sum(jnp.triu(1 / r_ee[..., 0], k=1))


def potential_electron_nuclear(
  charges: jnp.ndarray, r_ae: jnp.ndarray
) -> jnp.ndarray:
  """Returns the electron-nuclearpotential.

  Args:
    charges: Shape (natoms). Nuclear charges of the atoms.
    r_ae: Shape (nelectrons, natoms). r_ae[i, j] gives the distance between
      electron i and atom j.
  """
  return -jnp.sum(charges / r_ae[..., 0])


def potential_nuclear_nuclear(
  charges: jnp.ndarray, atoms: jnp.ndarray
) -> jnp.ndarray:
  """Returns the electron-nuclearpotential.

  Args:
    charges: Shape (natoms). Nuclear charges of the atoms.
    atoms: Shape (natoms, ndim). Positions of the atoms.
  """
  r_aa = jnp.linalg.norm(atoms[None, ...] - atoms[:, None], axis=-1)
  return jnp.sum(
    jnp.triu((charges[None, ...] * charges[..., None]) / r_aa, k=1)
  )


def potential_energy(
  r_ae: jnp.ndarray, r_ee: jnp.ndarray, atoms: jnp.ndarray, charges: jnp.ndarray
) -> jnp.ndarray:
  """Returns the potential energy for this electron configuration.

  Args:
    r_ae: Shape (nelectrons, natoms). r_ae[i, j] gives the distance between
      electron i and atom j.
    r_ee: Shape (neletrons, nelectrons, :). r_ee[i,j,0] gives the distance
      between electrons i and j. Other elements in the final axes are not
      required.
    atoms: Shape (natoms, ndim). Positions of the atoms.
    charges: Shape (natoms). Nuclear charges of the atoms.
  """
  return (
    potential_electron_electron(r_ee) +
    potential_electron_nuclear(charges, r_ae) +
    potential_nuclear_nuclear(charges, atoms)
  )


def local_energy(
  f: networks.WaveFuncLike,
  atoms: jnp.ndarray,
  charges: jnp.ndarray,
  nspins: Sequence[int],
  use_scan: bool = False,
  forward_laplacian=True
) -> LocalEnergy:
  """Creates the function to evaluate the local energy.

  Args:
    f: Callable which returns the sign and log of the magnitude of the
      wavefunction given the network parameters and configurations data.
    atoms: Shape (natoms, ndim). Positions of the atoms.
    charges: Shape (natoms). Nuclear charges of the atoms.
    nspins: Number of particles of each spin.
    use_scan: Whether to use a `lax.scan` for computing the laplacian.

  Returns:
    Callable with signature e_l(params, key, data) which evaluates the local
    energy of the wavefunction given the parameters params, RNG state key,
    and a single MCMC configuration in data.
  """
  del nspins
  log_abs_f = lambda *args, **kwargs: f(*args, **kwargs)[1]
  ke = local_kinetic_energy(
    log_abs_f, use_scan=use_scan, forward_laplacian=forward_laplacian
  )

  def _e_l(
    params: networks.ParamTree, key: chex.PRNGKey, data: jnp.ndarray
  ) -> jnp.ndarray:
    """Returns the total energy.

    Args:
      params: network parameters.
      key: RNG state.
      data: MCMC configuration.
    """
    # del key  # unused
    _, _, r_ae, r_ee = networks.construct_input_features(data, atoms)
    potential = potential_energy(r_ae, r_ee, atoms, charges)
    kinetic = ke(params, data, key)
    return potential + kinetic

  return _e_l
