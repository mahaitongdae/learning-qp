import torch
import torch.nn as nn
import numpy as np
import random
import gym
import pandas as pd
import os
from datetime import datetime
from utils.utils import bmv, bqf, bsolve
from icecream import ic

class LinearSystem():
    def __init__(self, A, B, Q, R, sqrt_W, x_min, x_max, u_min, u_max, bs, barrier_thresh, max_steps, u_eq_min=None, u_eq_max=None, device="cuda:0", random_seed=None, quiet=False, keep_stats=False, run_name="", **kwargs):
        """
        When keep_stats == True, statistics of previous episodes will be kept.
        """
        if random_seed is not None:
            torch.manual_seed(random_seed)
            torch.cuda.manual_seed_all(random_seed)
            np.random.seed(random_seed)
            random.seed(random_seed)
        self.device = device
        self.n = A.shape[0]
        self.m = B.shape[1]
        t = lambda a: torch.tensor(a, dtype=torch.float, device=device).unsqueeze(0)
        self.A = t(A)
        self.B = t(B)
        self.Q = t(Q)
        self.R = t(R)
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
        self.is_done = torch.zeros((bs,), dtype=torch.uint8, device=device)
        self.step_count = torch.zeros((bs,), dtype=torch.long, device=device)
        self.cum_cost = torch.zeros((bs,), dtype=torch.float, device=device)
        self.run_name = run_name
        self.keep_stats = keep_stats
        self.already_on_stats = torch.zeros((bs,), dtype=torch.uint8, device=device)   # Each worker can only contribute once to the statistics, to avoid bias towards shorter episodes
        self.stats = pd.DataFrame(columns=['x0', 'x_ref', 'episode_length', 'cumulative_cost', 'constraint_violated'])
        self.quiet = quiet

    def obs(self):
        return torch.cat([self.x, self.x_ref], -1)

    def cost(self, x, u):
        return bqf(x, self.Q) + bqf(u, self.R)

    def reward(self):
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
        return self.is_done.bool()

    def info(self):
        return {}

    def get_number_of_agents(self):
        return 1

    def get_num_parallel(self):
        return self.bs

    def generate_ref(self, size):
        u_ref = self.u_eq_min + (self.u_eq_max - self.u_eq_min) * torch.rand((size, self.m), device=self.device)
        x_ref = bsolve(torch.eye(self.n, device=self.device).unsqueeze(0) - self.A, bmv(self.B, u_ref))
        x_ref += self.barrier_thresh * torch.randn((size, self.n), device=self.device)
        x_ref = x_ref.clamp(self.x_min + self.barrier_thresh, self.x_max - self.barrier_thresh)
        return x_ref

    def reset_done_envs(self, need_reset=None, x=None, x_ref=None):
        is_done = self.is_done.bool() if need_reset is None else need_reset
        size = torch.sum(is_done)
        self.step_count[is_done] = 0
        self.cum_cost[is_done] = 0
        self.x_ref[is_done, :] = self.generate_ref(size) if x_ref is None else x_ref
        self.x0[is_done, :] = self.x_min + self.barrier_thresh + (self.x_max - self.x_min - 2 * self.barrier_thresh) * torch.rand((size, self.n), device=self.device) if x is None else x
        self.x[is_done, :] = self.x0[is_done, :]
        self.is_done[is_done] = 0

    def reset(self, x=None, x_ref=None):
        self.reset_done_envs(torch.ones(self.bs, dtype=torch.bool, device=self.device), x, x_ref)
        return self.obs()

    def check_in_bound(self):
        return ((self.x_min <= self.x) & (self.x <= self.x_max)).all(dim=-1)

    def write_episode_stats(self, i):
        """Write the stats of an episode to self.stats; call with the index in the batch when an episode is done."""
        self.already_on_stats[i] = 1
        x0 = self.x0[i, :].cpu().numpy()
        x_ref = self.x_ref[i, :].cpu().numpy()
        episode_length = self.step_count[i].item()
        cumulative_cost = self.cum_cost[i].item()
        constraint_violated = (self.is_done[i] == 1).item()
        self.stats.loc[len(self.stats)] = [x0, x_ref, episode_length, cumulative_cost, constraint_violated]

    def dump_stats(self, filename=None):
        if filename is None:
            directory = 'test_results'
            if not os.path.exists(directory):
                os.makedirs(directory)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            tag = self.run_name
            filename = os.path.join(directory, f"{tag}_{timestamp}.csv")
        self.stats.to_csv(filename, index=False)

    def step(self, u):
        self.reset_done_envs()
        self.cum_cost += self.cost(self.x - self.x_ref, u)
        self.step_count += 1
        u = u.clamp(self.u_min, self.u_max)
        self.u = u
        self.x = bmv(self.A, self.x) + bmv(self.B, u) + bmv(self.sqrt_W, torch.randn((self.bs, self.n), device=self.device))
        self.is_done[torch.logical_not(self.check_in_bound()).nonzero()] = 1   # 1 for failure
        self.is_done[self.step_count >= self.max_steps] = 2  # 2 for timeout
        if self.keep_stats:
            done_indices = torch.nonzero(self.is_done.to(dtype=torch.bool) & torch.logical_not(self.already_on_stats), as_tuple=False)
            for i in done_indices:
                self.write_episode_stats(i)
        return self.obs(), self.reward(), self.done(), self.info()

    def render(self, **kwargs):
        ic(self.x, self.x_ref, self.u)
        avg_cost = (self.cum_cost / self.step_count).cpu().numpy()
        ic(avg_cost)
