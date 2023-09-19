# %% Specify test case
import numpy as np

# Case where MPC is better
x0 = np.array([10., 10., 10., 10.])
x_ref = np.array([19, 19, 2.4, 2.4])   

# # Case where MPC fails
# x0 = np.array([ 5.4963946, 10.947876,   1.034516,  18.08066  ])
# x_ref = np.array([7.522859,  8.169776,  1.1107684, 1.       ])

# Controlling process noise and parametric uncertainty
noise_level = 0
parametric_uncertainty = False
parameter_randomization_seed = 2

# %% Set up test bench
import sys
import os
file_path = os.path.dirname(__file__)
sys.path.append(os.path.join(file_path, "../.."))

from envs.env_creators import sys_param, env_creators
from envs.mpc_baseline_parameters import get_mpc_baseline_parameters
from modules.qp_unrolled_network import QPUnrolledNetwork
import torch
from matplotlib import pyplot as plt
from icecream import ic


# Utilities

def make_obs(x, x_ref, running_mean, running_std, normalize):
    raw_obs = torch.tensor(np.concatenate([x, x_ref]), device=device, dtype=torch.float)
    if not normalize:
        return raw_obs.unsqueeze(0)
    else:
        return ((raw_obs - running_mean) / running_std).unsqueeze(0)

def get_state_dict(checkpoint_path):
    checkpoint = torch.load(checkpoint_path)
    model = checkpoint["model"]
    prefix = "a2c_network.policy_net."
    policy_net_state_dict = {k.lstrip(prefix): v for (k, v) in model.items() if k.startswith(prefix)}
    running_mean = model["running_mean_std.running_mean"].to(dtype=torch.float)
    running_std = model["running_mean_std.running_var"].sqrt().to(dtype=torch.float)
    return policy_net_state_dict, running_mean, running_std

def rescale_action(action, low=-1., high=8.):
    action = action.clamp(-1., 1.)
    return low + (high - low) * (action + 1) / 2

t = lambda arr: torch.tensor(arr, device=device, dtype=torch.float).unsqueeze(0)
a = lambda t: t.detach().cpu().numpy()

# Constants and options
n_sys = 4
m_sys = 2
input_size = 8   # 4 for x, 4 for x_ref
n = 2
m = 64
qp_iter = 10
device = "cuda:0"


# MPC module
mpc_baseline = get_mpc_baseline_parameters("tank", 1)
mpc_baseline["normalize"] = True   # Solve for normalized action, to be consistent with learned QP
mpc_module = QPUnrolledNetwork(
    device, input_size, n, m, qp_iter, None, True, True,
    mpc_baseline=mpc_baseline,
    use_osqp_for_mpc=True,
)

# Environment
env = env_creators["tank"](
    noise_level=noise_level,
    bs=1,
    max_steps=300,
    keep_stats=True,
    run_name="",
    exp_name="",
    randomize=parametric_uncertainty,
)

# %% Compare learned QPs learned with / without residual loss, and compare degree of constraint violation
from utils.utils import bmv

def get_qp_net(trained_with_residual_loss):
    exp_name = f"residual_loss_{'on' if trained_with_residual_loss else 'off'}"
    net = QPUnrolledNetwork(device, input_size, n, m, qp_iter, None, True, True)
    if parametric_uncertainty:
        exp_name += "+rand"
    checkpoint_path = f"runs/tank_{exp_name}/nn/tank.pth"
    policy_net_state_dict, running_mean, running_std = get_state_dict(checkpoint_path)
    net.load_state_dict(policy_net_state_dict)
    running_mean, running_std = running_mean.to(device=device), running_std.to(device=device)
    net.to(device)
    return net, running_mean, running_std

def compute_violation(H, action_all, b):
    """
    Number of violated constraints, as well as magnitude of constraint violation.
    """
    z_recovered = bmv(H, action_all) + b
    violation_count = (z_recovered < 0.).sum(dim=-1)
    violation_magnitude = torch.norm(z_recovered.clamp(-torch.inf, 0.), dim=-1)
    return violation_count, violation_magnitude

def rollout(trained_with_residual_loss, is_mpc, steps):
    net, running_mean, running_std = get_qp_net(trained_with_residual_loss)
    if is_mpc:
        net = mpc_module
    results = []
    env.reset(t(x0), t(x_ref), randomize_seed=parameter_randomization_seed)
    x = x0
    obs = make_obs(x, x_ref, running_mean, running_std, not is_mpc)
    for i in range(steps):
        action_all, problem_params = net(obs, return_problem_params=True)
        u = rescale_action(action_all[:, :m_sys])
        raw_obs, reward, done_t, info = env.step(u)
        if not is_mpc:
            obs = (raw_obs - running_mean) / running_std
        else:
            obs = raw_obs
        done = done_t.item()
        P, q, H, b = problem_params
        results.append((P, q, H, b, action_all))
    return results

def evaluate_constraint_violation(trained_with_residual_loss, steps=10):
    """Rollout for multiple steps, and compute average (number of violated constraints, magnitude of violation)."""
    rollout_results = rollout(trained_with_residual_loss, False, steps)
    constraint_violation_indices = []
    for i in range(steps):
        H = rollout_results[i][2]
        action_all = rollout_results[i][4]
        b = rollout_results[i][3]
        constraint_violation_indices.append(compute_violation(H, action_all, b))
    average_violation_count = torch.stack([v[0] for v in constraint_violation_indices], dim=0).to(dtype=torch.float).mean(dim=0)
    average_violation_magnitude = torch.stack([v[1] for v in constraint_violation_indices], dim=0).mean(dim=0)
    return average_violation_count, average_violation_magnitude

violation_count_with_residual_loss, violation_magnitude_with_residual_loss = evaluate_constraint_violation(True)
violation_count_without_residual_loss, violation_magnitude_without_residual_loss = evaluate_constraint_violation(False)

ic(violation_count_with_residual_loss, violation_count_without_residual_loss)
ic(violation_magnitude_with_residual_loss, violation_magnitude_without_residual_loss)

# %% Visualize the feasible set and objective function at a certain step, ignoring constraints that are violated
at_step = 3

from utils.visualization import plot_multiple_2d_polytopes_with_contour

def get_violated_mask(H, action_all, b):
    z_recovered = bmv(H, action_all) + b
    return torch.where(z_recovered < 0., torch.ones_like(z_recovered), torch.zeros_like(z_recovered))

def get_step_parameters(at_step, trained_with_residual_loss, is_mpc):
    rollout_results = rollout(trained_with_residual_loss, is_mpc, at_step)
    result_last_step = rollout_results[-1]
    P, q, H, b, action_all = result_last_step
    violated_mask = get_violated_mask(H, action_all, b)
    return P, q, H, b, violated_mask, action_all

def get_plot_parameters(trained_with_residual_loss, is_mpc, color, label):
    a = lambda t: t.squeeze(0).detach().cpu().numpy()
    global P, q, H, b, violated_mask, action_all
    P, q, H, b, violated_mask, action_all = get_step_parameters(at_step, trained_with_residual_loss, is_mpc)
    satisfied_mask = torch.logical_not(violated_mask)
    plot_params = {
        "A": a(-H[satisfied_mask, :]),
        "b": a(b[satisfied_mask]),
        "optimal_solution": a(action_all[:, :m_sys]),
        "P": a(P),
        "q": a(q),
        "color": color,
        "label": label,
    }
    return plot_params

fig, ax = plot_multiple_2d_polytopes_with_contour([
    get_plot_parameters(True, False, "blue", "Learned QP (with residual loss)"),
    get_plot_parameters(False, False, "red", "Learned QP (w/o residual loss)"),
    get_plot_parameters(False, True, "green", "MPC")
])
ax.set_xlabel("$u_1$")
ax.set_ylabel("$u_2$")
ax.set_title(f"Feasible sets and objective functions at step {at_step}")


# %%
