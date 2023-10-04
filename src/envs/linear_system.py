import torch
import torch.nn as nn
import numpy as np
import random
import gym
import pandas as pd
import os
from datetime import datetime
from ..utils.torch_utils import bmv, bqf, bsolve, conditional_fork_rng, get_rng
from icecream import ic


class LinearSystem():
    def __init__(self, A, B, Q, R, sqrt_W, x_min, x_max, u_min, u_max, bs, barrier_thresh, max_steps, u_eq_min=None, u_eq_max=None, device="cuda:0", random_seed=None, quiet=False, keep_stats=False, randomize_std=0., run_name="", **kwargs):
        """
        Initializes the LinearSystem environment with given parameters.
        
        Parameters:
            A (ndarray): System dynamics matrix. Perturbed with standard deviation randomize_std.
            B (ndarray): Input matrix. Perturbed with standard deviation randomize_std.
            Q (ndarray): State cost matrix.
            R (ndarray): Control input cost matrix.
            sqrt_W (ndarray): Square root of the process noise covariance matrix.
            x_min (ndarray): Lower bound for each state variable.
            x_max (ndarray): Upper bound for each state variable.
            u_min (ndarray): Lower bound for each control input.
            u_max (ndarray): Upper bound for each control input.
            bs (int): Batch size for parallel environment execution.
            barrier_thresh (float): Threshold for state constraint barriers.
            max_steps (int): Maximum number of steps in an episode.
            u_eq_min (ndarray, optional): Lower bound for equilibrium control input.
            u_eq_max (ndarray, optional): Upper bound for equilibrium control input.
            device (str, optional): Computational device ("cpu" or "cuda").
            random_seed (int, optional): Random seed for reproducibility.
            quiet (bool, optional): Suppresses debug prints when set to True.
            keep_stats (bool, optional): Whether to maintain statistics of episodes.
            randomize_std (float, optional): Standard deviation for perturbation to A, B matrices.
            run_name (str, optional): Name tag for the run, useful for logging.
        """
        # Random seed and random number generators for different components
        if random_seed is not None:
            torch.manual_seed(random_seed)
            torch.cuda.manual_seed_all(random_seed)
            np.random.seed(random_seed)
            random.seed(random_seed)
        # Random number generator for initial states and references
        self.rng_initial = get_rng(device, random_seed)
        # Random number generator for process noise
        self.rng_process = get_rng(device, random_seed)
        # Random number generator for randomization of A, B matrices
        self.rng_dynamics = get_rng(device, random_seed)

        # Environment definitions
        self.device = device
        self.n = A.shape[0]
        self.m = B.shape[1]
        t = lambda a: torch.tensor(a, dtype=torch.float, device=device).unsqueeze(0)
        self.A = t(A)
        self.B = t(B)
        self.Q = t(Q)
        self.R = t(R)
        self.A0 = self.A
        self.B0 = self.B
        self.randomize_std = randomize_std
        if randomize_std > 0:
            # Copy the nominal A, B, and repeat A, B along the batch dimension to allow randomization later
            self.A = self.A.repeat(bs, 1, 1)
            self.B = self.B.repeat(bs, 1, 1)
        self.sqrt_W = t(sqrt_W)
        self.x_min = t(x_min)
        self.x_max = t(x_max)
        self.u_min = t(u_min)
        self.u_max = t(u_max)
        self.u_eq_min = t(u_eq_min) if u_eq_min is not None else self.u_min
        self.u_eq_max = t(u_eq_max) if u_eq_max is not None else self.u_max
        self.bs = bs
        self.barrier_thresh = barrier_thresh
        self.max_steps = max_steps
        self.num_states = self.n
        self.num_actions = self.m
        self.observation_space = gym.spaces.Box(low=-np.inf, high=np.inf, shape=(2 * self.n,))
        self.action_space = gym.spaces.Box(low=u_min, high=u_max, shape=(self.m,))
        self.state_space = self.observation_space
        self.x = 0.5 * (self.x_max + self.x_min) * torch.ones((bs, self.n), device=device)
        self.x0 = 0.5 * (self.x_max + self.x_min) * torch.ones((bs, self.n), device=device)
        self.u = torch.zeros((bs, self.m), device=device)
        self.x_ref = 0.5 * (self.x_max + self.x_min) * torch.ones((bs, self.n), device=device)
        # Keep record of the first noise vector in each trajectory, as an identifier of the instantiation of the randomness
        self.w0 = torch.zeros_like(self.x)
        self.is_done = torch.zeros((bs,), dtype=torch.uint8, device=device)
        self.step_count = torch.zeros((bs,), dtype=torch.long, device=device)
        self.cum_cost = torch.zeros((bs,), dtype=torch.float, device=device)
        self.run_name = run_name
        self.keep_stats = keep_stats
        self.already_on_stats = torch.zeros((bs,), dtype=torch.uint8, device=device)   # Each worker can only contribute once to the statistics, to avoid bias towards shorter episodes
        self.stats = pd.DataFrame(columns=['i', 'x0', 'x_ref', 'A', 'B', 'w0', 'episode_length', 'cumulative_cost', 'constraint_violated'])
        self.quiet = quiet

    def obs(self):
        """
        Returns the current observation, which is a concatenation of the current state and reference state.
        """
        return torch.cat([self.x, self.x_ref], -1)

    def cost(self, x, u):
        """
        Computes the cost based on the state deviation from the reference and control effort.
        """
        return bqf(x, self.Q) + bqf(u, self.R)

    def reward(self):
        """
        Computes the reward based on the current state, control input, and various coefficients.
        """
        rew_main = -self.cost(self.x - self.x_ref, self.u)
        rew_state_bar = torch.sum(torch.log(((self.x_max - self.x) / self.barrier_thresh).clamp(1e-8, 1.)) + torch.log(((self.x - self.x_min) / self.barrier_thresh).clamp(1e-8, 1.)), dim=-1)
        rew_done = -1.0 * (self.is_done == 1)

        coef_const = 0.
        coef_main = 1.
        coef_bar = 0.
        coef_done = 100000.

        rew_total = coef_const + coef_main * rew_main + coef_bar * rew_state_bar + coef_done * rew_done

        if not self.quiet:
            avg_rew_main, avg_rew_state_bar, avg_rew_done, avg_rew_total = coef_main * rew_main.mean().item(), coef_bar * rew_state_bar.mean().item(), coef_done * rew_done.mean().item(), rew_total.mean().item()
            ic(avg_rew_main, avg_rew_done, avg_rew_total)
        return rew_total

    def done(self):
        """
        Checks whether the episode has terminated for each environment in the batch.
        """
        return self.is_done.bool()

    def info(self):
        """
        Returns an empty dictionary, serving as a placeholder for additional information.
        """
        return {}

    def get_number_of_agents(self):
        """
        Returns the number of agents in the environment, which is 1 in this case.
        """
        return 1

    def get_num_parallel(self):
        """
        Returns the batch size for parallel environment execution.
        """
        return self.bs

    def generate_ref(self, size):
        """
        Generates a reference state based on the control input bounds and nominal system dynamics.
        """
        u_ref = self.u_eq_min + (self.u_eq_max - self.u_eq_min) * torch.rand((size, self.m), generator=self.rng_initial, device=self.device)
        x_ref = bsolve(torch.eye(self.n, device=self.device).unsqueeze(0) - self.A0, bmv(self.B0, u_ref))
        x_ref += self.barrier_thresh * torch.randn((size, self.n), generator=self.rng_initial, device=self.device)
        x_ref = x_ref.clamp(self.x_min + self.barrier_thresh, self.x_max - self.barrier_thresh)
        return x_ref

    def reset_done_envs(self, need_reset=None, x=None, x_ref=None, randomize_seed=None):
        """
        Resets the environments that are marked as 'done', reinitializing their states and references.

        Parameters:
        - need_reset (torch.Tensor, optional): A boolean tensor indicating which environments need to be reset. 
                                            If None, the function will automatically determine this based on self.is_done.
        - x (torch.Tensor, optional): Initial state tensor for the environments that need to be reset.
                                    If None, random initial states are generated within defined bounds.
        - x_ref (torch.Tensor, optional): Reference state tensor for the environments that need to be reset.
                                        If None, references are generated via self.generate_ref().
        - randomize_seed (int, optional): Seed for random number generation when randomizing system matrices A and B.
                                        If None, no seeding is applied.

        Side Effects:
        - Modifies self.step_count, self.cum_cost, self.x_ref, self.x0, self.x, self.is_done, self.A, and self.B for the 
        environments that are reset.

        Notes:
        - The function expects self.is_done, self.x_min, self.x_max, self.barrier_thresh, self.n, self.m, self.device, 
        self.randomize_std, self.A0, and self.B0 to be pre-defined.
        - Utilizes the conditional_fork_rng context manager for conditional seeding.
        """
        is_done = self.is_done.bool() if need_reset is None else need_reset
        size = torch.sum(is_done)
        self.step_count[is_done] = 0
        self.cum_cost[is_done] = 0
        self.x_ref[is_done, :] = self.generate_ref(size) if x_ref is None else x_ref
        self.x0[is_done, :] = self.x_min + self.barrier_thresh + (self.x_max - self.x_min - 2 * self.barrier_thresh) * torch.rand((size, self.n), generator=self.rng_initial, device=self.device) if x is None else x
        self.x[is_done, :] = self.x0[is_done, :]
        self.is_done[is_done] = 0
        if self.randomize_std > 0:
            if randomize_seed is not None:
                # Seed for randomization of dynamics is specified in function all; use it directly
                with torch.random.fork_rng():
                    torch.manual_seed(randomize_seed)
                    noise_A = torch.randn((size, self.n, self.n), device=self.device) * self.randomize_std
                    noise_B = torch.randn((size, self.n, self.m), device=self.device) * self.randomize_std
            else:
                # No seed specified; use predefined random number generator for randomization of dynamics
                noise_A = torch.randn((size, self.n, self.n), generator=self.rng_dynamics, device=self.device) * self.randomize_std
                noise_B = torch.randn((size, self.n, self.m), generator=self.rng_dynamics, device=self.device) * self.randomize_std
            self.A[is_done, :, :] = self.A0 + noise_A
            self.B[is_done, :, :] = self.B0 + noise_B


    def reset(self, x=None, x_ref=None, randomize_seed=None):
        """
        Resets the environment, reinitializing the states and references.
        """
        self.reset_done_envs(torch.ones(self.bs, dtype=torch.bool, device=self.device), x, x_ref, randomize_seed)
        return self.obs()

    def check_in_bound(self):
        """
        Checks whether the current state is within the predefined bounds.
        """
        return ((self.x_min <= self.x) & (self.x <= self.x_max)).all(dim=-1)

    def write_episode_stats(self, i):
        """
        Logs statistics of the episode for the ith environment in the batch.
        """
        self.already_on_stats[i] = 1
        x0 = self.x0[i, :].cpu().numpy()
        x_ref = self.x_ref[i, :].cpu().numpy()

        # Get the A and B matrices and flatten for the ith environment
        index = 0 if self.randomize_std == 0 else i
        A = self.A[index, :, :].cpu().numpy().flatten()
        B = self.B[index, :, :].cpu().numpy().flatten()

        w0 = self.w0[i, :].cpu().numpy()

        episode_length = self.step_count[i].item()
        cumulative_cost = self.cum_cost[i].item()
        constraint_violated = (self.is_done[i] == 1).item()
        self.stats.loc[len(self.stats)] = [i.item(), x0, x_ref, A, B, w0, episode_length, cumulative_cost, constraint_violated]

    def dump_stats(self, filename=None):
        """
        Writes the accumulated statistics to a CSV file.
        """
        if filename is None:
            directory = 'test_results'
            if not os.path.exists(directory):
                os.makedirs(directory)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            tag = self.run_name
            filename = os.path.join(directory, f"{tag}_{timestamp}.csv")
        self.stats = self.stats.sort_values(by='i')
        self.stats.to_csv(filename, index=False)

    def step(self, u):
        """
        Executes one step in the environment based on the given control input.
        """
        self.reset_done_envs()
        u = u.clamp(self.u_min, self.u_max)
        self.u = u
        self.cum_cost += self.cost(self.x - self.x_ref, u)
        w = bmv(self.sqrt_W, torch.randn((self.bs, self.n), generator=self.rng_process, device=self.device))
        self.w0[self.step_count == 0, :] = w[self.step_count == 0, :]
        self.x = bmv(self.A, self.x) + bmv(self.B, u) + w
        self.step_count += 1
        self.is_done[torch.logical_not(self.check_in_bound()).nonzero()] = 1   # 1 for failure
        self.is_done[self.step_count >= self.max_steps] = 2  # 2 for timeout
        if self.keep_stats:
            done_indices = torch.nonzero(self.is_done.to(dtype=torch.bool) & torch.logical_not(self.already_on_stats), as_tuple=False)
            for i in done_indices:
                self.write_episode_stats(i)
        return self.obs(), self.reward(), self.done(), self.info()

    def render(self, **kwargs):
        """
        Prints the current state, reference state, and control input for debugging purposes.
        """
        ic(self.x, self.x_ref, self.u)
        avg_cost = (self.cum_cost / self.step_count).cpu().numpy()
        ic(avg_cost)