"""
Extra tools that are useful in hydrodynamical problems.

"""

import numpy as np
from mpi4py import MPI

from dedalus.core import operators

import logging
logger = logging.getLogger(__name__.split('.')[-1])


class GlobalArrayReducer:
    """
    Directs parallelized reduction of distributed array data.

    Parameters
    ----------
    comm : MPI communicator
        MPI communicator
    dtype : data type, optional
        Array data type (default: np.float64)

    """

    def __init__(self, comm, dtype=np.float64):

        self.comm = comm
        self._scalar_buffer = np.zeros(1, dtype=dtype)

    def reduce_scalar(self, local_scalar, mpi_reduce_op):
        """Compute global reduction of a scalar from each process."""
        self._scalar_buffer[0] = local_scalar
        self.comm.Allreduce(MPI.IN_PLACE, self._scalar_buffer, op=mpi_reduce_op)
        return self._scalar_buffer[0]

    def global_min(self, data, empty=np.inf):
        """Compute global min of all array data."""
        if data.size:
            local_min = np.min(data)
        else:
            local_min = empty
        return self.reduce_scalar(local_min, MPI.MIN)

    def global_max(self, data, empty=-np.inf):
        """Compute global max of all array data."""
        if data.size:
            local_max = np.max(data)
        else:
            local_max = empty
        return self.reduce_scalar(local_max, MPI.MAX)

    def global_mean(self, data):
        """Compute global mean of all array data."""
        local_sum = np.sum(data)
        local_size = data.size
        global_sum = self.reduce_scalar(local_sum, MPI.SUM)
        global_size = self.reduce_scalar(local_size, MPI.SUM)
        return global_sum / global_size


class GlobalFlowProperty:
    """
    Directs parallelized determination of a global flow property on the grid.

    Parameters
    ----------
    solver : solver object
        Problem solver
    cadence : int, optional
        Iteration cadence for property evaluation (default: 1)

    Examples
    --------
    >>> flow = GlobalFlowProperty(solver)
    >>> flow.add_property('sqrt(u*u + w*w) * Lz / nu', name='Re')
    ...
    >>> flow.max('Re')
    1024.5

    """

    def __init__(self, solver, cadence=1):

        self.solver = solver
        self.cadence = cadence
        self.reducer = GlobalArrayReducer(solver.dist.comm_cart)
        self.properties = solver.evaluator.add_dictionary_handler(iter=cadence)

    def add_property(self, property, name, precompute_integral=False):
        """Add a property."""
        self.properties.add_task(property, layout='g', name=name)
        if precompute_integral:
            # Add integral under slightly obscured name
            task_op = self.properties.tasks[-1]['operator']
            integral_op = operators.Integrate(task_op)
            integral_name = '_{}_integral'.format(name)
            self.properties.add_task(integral_op, layout='g', name=integral_name)

    def min(self, name):
        """Compute global min of a property on the grid."""
        gdata = self.properties[name]['g']
        return self.reducer.global_min(gdata)

    def max(self, name):
        """Compute global max of a property on the grid."""
        gdata = self.properties[name]['g']
        return self.reducer.global_max(gdata)

    def grid_average(self, name):
        """Compute global mean of a property on the grid."""
        gdata = self.properties[name]['g']
        return self.reducer.global_mean(gdata)

    def volume_integral(self, name):
        """Compute volume integral of a property."""
        # Check for precomputed integral
        try:
            integral_name = '_{}_integral'.format(name)
            integral_field = self.properties[integral_name]
        except KeyError:
            # Compute volume integral
            field = self.properties[name]
            integral_op = operators.Integrate(field)
            integral_field = integral_op.evaluate()
        # Communicate integral value to all processes
        integral_value = self.reducer.global_max(integral_field['g'])
        return integral_value

    def volume_average(self, name):
        """Compute volume average of a property."""
        # TODO: missing hypervolume definition
        raise NotImplementedError("missing definition of hypervolume")
        average_value = self.volume_integral(name) / self.solver.domain.hypervolume
        return average_value

class CFL:
    """
    Computes CFL-limited timestep from a set of frequencies/velocities.

    Parameters
    ----------
    solver : solver object
        Problem solver
    initial_dt : float
        Initial timestep
    cadence : int, optional
        Iteration cadence for computing new timestep (default: 1)
    safety : float, optional
        Safety factor for scaling computed timestep (default: 1.)
    max_dt : float, optional
        Maximum allowable timestep (default: inf)
    min_dt : float, optional
        Minimum allowable timestep (default: 0.)
    max_change : float, optional
        Maximum fractional change between timesteps (default: inf)
    min_change : float, optional
        Minimum fractional change between timesteps (default: 0.)
    threshold : float, optional
            Fractional change threshold for changing timestep (default: 0.)

    Notes
    -----
    The new timestep is computed by summing across the provided frequencies
    for each grid point, and then reciprocating the maximum "total" frequency
    from the entire grid.

    """

    def __init__(self, solver, initial_dt, cadence=1, safety=1., max_dt=np.inf,
                 min_dt=0., max_change=np.inf, min_change=0., threshold=0.):
        self.solver = solver
        self.stored_dt = initial_dt
        self.cadence = cadence
        self.safety = safety
        self.max_dt = max_dt
        self.min_dt = min_dt
        self.max_change = max_change
        self.min_change = min_change
        self.threshold = threshold

        self.reducer = GlobalArrayReducer(self.solver.dist.comm_cart)
        self.frequencies = self.solver.evaluator.add_dictionary_handler(iter=cadence)

    def compute_dt(self):
        logger.warning("'CFL.compute_dt' is deprecated. Use 'CFL.compute_timestep' instead.")
        return self.compute_timestep()

    def compute_timestep(self):
        """Compute CFL-limited timestep."""
        iteration = self.solver.iteration
        # Compute new timestep when cadence divides previous iteration
        # (this is when the frequency dicthandler is freshly updated)
        if (iteration-1) % self.cadence == 0:
            # Return initial dt on first evaluation
            if (iteration-1) <= self.solver.initial_iteration:
                return self.stored_dt
            # Sum across frequencies for each local grid point
            local_freqs = np.sum(np.abs(field['g']) for field in self.frequencies.fields.values())
            # Compute new timestep from max frequency across all grid points
            max_global_freq = self.reducer.global_max(local_freqs)
            if max_global_freq == 0.:
                dt = np.inf
            else:
                dt = 1 / max_global_freq
            # Apply restrictions
            dt *= self.safety
            dt = min(dt, self.max_dt, self.max_change*self.stored_dt)
            dt = max(dt, self.min_dt, self.min_change*self.stored_dt)
            if abs(dt - self.stored_dt) > self.threshold * self.stored_dt:
                self.stored_dt = dt
        return self.stored_dt

    def add_frequency(self, freq):
        """Add an on-grid frequency."""
        self.frequencies.add_task(freq, layout='g', scales=freq.domain.dealias)

    def add_velocity(self, velocity):
        """
        Add grid-crossing frequency from a velocity vector.

        Parameters
        ---------
        velocity : field object
            The velocity; must be a vector with a tensorsig of length 1
        """
        coords = velocity.tensorsig
        if len(coords) != 1:
            raise ValueError("Velocity must be a vector")
        cfl_operator = operators.AdvectiveCFL(velocity, coords[0])
        self.add_frequency(cfl_operator)
