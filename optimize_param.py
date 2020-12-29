"""
The purpose of this file is to load in different trajectories and compare how model types predict control performance.
There are three options:
- Gausian process mapping from control policy and s0 to reward
- One step model rolling out predicted trajectory from initial state, cumulative reward
- trajectory model predicting trajectory and cumulative reward
"""

import sys

import hydra
import logging
import itertools

import gym
import torch
import numpy as np
import cma

from plot import *
from evaluate import test_models
import gpytorch
from mbrl_resources import obs2q
from policy import LQR
from reacher_pd import run_controller

from ax import (
    ComparisonOp,
    ParameterType, Parameter, RangeParameter, ChoiceParameter,
    FixedParameter, OutcomeConstraint, SimpleExperiment, Models,
    Arm, Metric, Runner, OptimizationConfig, Objective, Data,
    SearchSpace
)
from ax.plot.trace import optimization_trace_single_method
from ax.plot.contour import plot_contour

import numpy as np
import pandas as pd
import plotly

import os
import sys
import hydra
import logging

# import plotly.graph_objects as go
# from plot import save_fig, plot_learning

# add cwd to path to allow running directly from the repo top level directory
sys.path.append(os.getcwd())
log = logging.getLogger(__name__)


def gen_search_space(cfg):
    l = []
    for key, item in cfg.space.items():
        if item.value_type == 'float':
            typ = ParameterType.FLOAT
        elif item.value_type == 'int':
            typ = ParameterType.INT
        elif item.value_type == 'bool':
            typ == Parameter.BOOL
        else:
            raise ValueError("invalid search space value type")

        if item.type == 'range':
            ss = RangeParameter(
                name=key, parameter_type=typ, lower=item.bounds[0], upper=item.bounds[1], log_scale=item.log_scale,
            )
        elif item.type == 'fixed':
            ss = FixedParameter(name=key, value=item.bounds, parameter_type=typ)
        elif item.type == 'choice':
            ss = ChoiceParameter(name=key, parameter_type=typ, values=item.bounds)
        else:
            raise ValueError("invalid search space parameter type")
        l.append(ss)
    return l


def get_reward_reacher(state, action):
    # Copied from the reacher env, without self.state calls
    vec = state[-3:]
    reward_dist = - np.linalg.norm(vec)
    reward_ctrl = - np.square(action).sum() * 0.01
    reward = reward_dist  # + reward_ctrl
    return reward


def get_reward_cp(state, action):
    # custom reward for sq error from x=0, theta = 0
    reward = state[0] ** 2 + state[2] ** 2
    return -reward


def get_reward_cf(state, action):
    # custom reward for sq error from x=0, theta = 0
    # reward = np.cos(state[4])*np.cos(state[3])
    # if (np.rad2deg(state[3]) < 5) and (np.rad2deg(state[4]) < 5):
    #     reward = 1
    # else:
    #     reward = 0
    reward = -state[3] ** 2 - state[4] ** 2
    return reward


def get_reward(predictions, actions, r_function):
    # takes in the predicted trajectory and returns the reward
    rewards = {}
    num_traj = len(actions)
    for m_label, state_data in predictions.items():
        r = []
        for i in range(num_traj):
            r_sub = 0
            cur_states = state_data[i]
            cur_actions = actions[i]
            for s, a in zip(cur_states, cur_actions):
                # TODO need a specific get reward function for the reacher env
                r_sub += r_function(s, a)
            r.append(r_sub)
        rewards[m_label] = (r, np.mean(r), np.std(r))

    return rewards


def pred_traj(test_data, models, control=None, env=None, cfg=None, t_range=None):
    # for a one-step model, predicts a trajectory from initial state all in simulation
    log.info("Beginning testing of predictions")

    states, actions, initials = [], [], []

    if env == 'reacher' or env == 'crazyflie':
        P, D, target = [], [], []

        # Compile the various trajectories into arrays
        for traj in test_data:
            states.append(traj.states)
            actions.append(traj.actions)
            initials.append(traj.states[0, :])
            P.append(traj.P)
            D.append(traj.D)
            target.append(traj.target)

        P_param = np.array(P)
        P_param = P_param.reshape((len(test_data), -1))
        D_param = np.array(D)
        D_param = D_param.reshape((len(test_data), -1))
        target = np.array(target)
        target = target.reshape((len(test_data), -1))

        parameters = [[P[0], 0, D[0]],
                      [P[1], 0, D[1]]]
        if env == 'crazyflie':
            from crazyflie_pd import PidPolicy
            policy = PidPolicy(parameters, cfg.pid)
            policies = []
            for p, d in zip(P_param, D_param):
                policies.append(PidPolicy([[p[0], 0, d[0]], [p[1], 0, d[1]]], cfg.pid))
            # policies = [LQR(A, B.transpose(), Q, R, actionBounds=[-1.0, 1.0]) for i in range(len(test_data))]


    elif env == 'cartpole':
        K = []

        # Compile the various trajectories into arrays
        for traj in test_data:
            states.append(traj.states)
            actions.append(traj.actions)
            initials.append(traj.states[0, :])
            K.append(traj.K)

        K_param = np.array(K)
        K_param = K_param.reshape((len(test_data), -1))

        # create LQR controllers to propogate predictions in one-step
        from policy import LQR

        # These values are replaced an don't matter
        m_c = 1
        m_p = 1
        m_t = m_c + m_p
        g = 9.8
        l = .01
        A = np.array([
            [0, 1, 0, 0],
            [0, g * m_p / m_c, 0, 0],
            [0, 0, 0, 1],
            [0, 0, g * m_t / (l * m_c), 0],
        ])
        B = np.array([
            [0, 1 / m_c, 0, -1 / (l * m_c)],
        ])
        Q = np.diag([.5, .05, 1, .05])
        R = np.ones(1)

        n_dof = np.shape(A)[0]
        modifier = .5 * np.random.random(
            4) + 1  # np.random.random(4)*1.5 # makes LQR values from 0% to 200% of true value
        policies = [LQR(A, B.transpose(), Q, R, actionBounds=[-1.0, 1.0]) for i in range(len(test_data))]
        for p, K in zip(policies, K_param):
            p.K = K

    # Convert to numpy arrays
    states = np.stack(states)
    actions = np.stack(actions)

    initials = np.array(initials)
    N, T, D = states.shape
    if len(np.shape(actions)) == 2:
        actions = np.expand_dims(actions, axis=2)
    # Iterate through each type of model for evaluation
    predictions = {key: [states[:, 0, models[key].state_indices]] for key in models}
    currents = {key: states[:, 0, models[key].state_indices] for key in models}

    ind_dict = {}
    for i, key in list(enumerate(models)):
        model = models[key]
        if model.traj:
            raise ValueError("Traj model conditioned on predicted states is invalid")
        indices = model.state_indices
        traj = model.traj

        ind_dict[key] = indices

        for i in range(1, T):
            if i >= t_range:
                continue
            # if control:
            # policy = PID(dX=5, dU=5, P=P, I=I, D=D, target=target)
            # act, t = control.act(obs2q(currents[key]))
            if env == 'crazyflie':
                acts = np.stack([[p.get_action(currents[key][i, 3:6])] for i, p in enumerate(policies)]).reshape(-1, 4)
            else:
                acts = np.stack([[p.act(obs2q(currents[key][i, :]))[0]] for i, p in enumerate(policies)])

            prediction = model.predict(np.hstack((currents[key], acts)))
            prediction = np.array(prediction.detach())

            predictions[key].append(prediction)
            currents[key] = prediction.squeeze()

    predictions = {key: np.array(predictions[key]).transpose([1, 0, 2]) for key in predictions}
    # MSEs = {key: np.square(states[:, :, ind_dict[key]] - predictions[key]).mean(axis=2)[:, 1:] for key in predictions}

    return 0, predictions


def train_gp(data):
    class ExactGPModel(gpytorch.models.ExactGP):
        def __init__(self, train_x, train_y, likelihood):
            super(ExactGPModel, self).__init__(train_x, train_y, likelihood)
            self.mean_module = gpytorch.means.ConstantMean()
            # self.covar_module = gpytorch.kernels.RBFKernel()
            # self.scaled_mod = gpytorch.kernels.ScaleKernel(self.covar_module)
            self.covar_module = gpytorch.kernels.ScaleKernel(gpytorch.kernels.RBFKernel())

        def forward(self, x):
            mean_x = self.mean_module(x)
            covar_x = self.covar_module(x)
            # covar_x = self.scaled_mod(x)
            return gpytorch.distributions.MultivariateNormal(mean_x, covar_x)

    # initialize likelihood and model
    likelihood = gpytorch.likelihoods.GaussianLikelihood()
    train_x = data[0]
    train_y = data[1]
    model = ExactGPModel(train_x, train_y, likelihood)

    # Find optimal model hyperparameters
    model.train()
    likelihood.train()

    # Use the adam optimizer
    optimizer = torch.optim.Adam([
        {'params': model.parameters()},  # Includes GaussianLikelihood parameters
    ], lr=.1)  # was .1

    # "Loss" for GPs - the marginal log likelihood
    mll = gpytorch.mlls.ExactMarginalLogLikelihood(likelihood, model)

    training_iter = 50
    for i in range(training_iter):
        # Zero gradients from previous iteration
        optimizer.zero_grad()
        # Output from model
        output = model(train_x)
        # Calc loss and backprop gradients
        loss = -mll(output, train_y)
        loss.backward()
        print('Iter %d/%d - Loss: %.3f   lengthscale: %.3f   noise: %.3f' % (
            i + 1, training_iter, loss.item(),
            model.covar_module.base_kernel.lengthscale.item(),
            model.likelihood.noise.item()
        ))
        optimizer.step()

    return model, likelihood


def eval_cp_model(parameters):
    k_param = [parameters["k1"], parameters["k2"], parameters["k3"], parameters["k4"]]
    env = gym.make("Cartpole-v0")
    rews = []
    for i in range(20):
        s0 = env.reset()
        t_range = np.arange(1, 200, 1)

        s_tile = np.tile(s0, 199).reshape(199, -1)
        k_tile = np.tile(k_param, 199).reshape(199, -1)
        input = np.concatenate((s_tile, t_range.reshape(-1, 1), k_tile), axis=1)
        states = traj_model.predict(input)
        traj = np.concatenate((s0.reshape(1, -1), states.numpy()), axis=0)
        rew = 0  # get_reward(np.concatenate((s0.reshape(1,-1),states.numpy()),axis=0),np.zeros(200,1),get_reward_cp)
        for t in traj:
            rew += np.exp(get_reward_cp(t, 0)) / 200
        rews.append(rew)

    log.info(
        f"Parameter eval in model {np.round(k_param, 3)} achieved r {np.round(np.mean(rews), 3)}, var {np.round(np.std(rews), 3)}")
    return {"Reward": (np.mean(rews), np.max(np.std(rews)),0.01), }

def eval_cp_model_scaled(parameters):
    # TODO scale the parameters for CMA opt
    k_param = parameters #[parameters["k1"], parameters["k2"], parameters["k3"], parameters["k4"]]
    # shift and scaling
    k_param += [-1, -5, -12, -10]
    k_param = np.multiply([1,2,4,2],k_param)
    if np.any(k_param>0) or np.any(k_param<-75):
        rews =[1000]
    else:
    # k_param[0] = k_param[0] -1
    # k_param[1] = 4*k_param[1] - 4
    # k_param[2] = 20*k_param[2] - 50
    # k_param[3] = 5*k_param[3] - 10
        env = gym.make("Cartpole-v0")
        rews = []
        for i in range(20):
            s0 = env.reset()
            t_range = np.arange(1, 200, 1)

            s_tile = np.tile(s0, 199).reshape(199, -1)
            k_tile = np.tile(k_param, 199).reshape(199, -1)
            input = np.concatenate((s_tile, t_range.reshape(-1, 1), k_tile), axis=1)
            states = traj_model.predict(input)
            traj = np.concatenate((s0.reshape(1, -1), states.numpy()), axis=0)
            rew = 0  # get_reward(np.concatenate((s0.reshape(1,-1),states.numpy()),axis=0),np.zeros(200,1),get_reward_cp)
            for t in traj:
                rew += np.exp(get_reward_cp(t, 0)) / 200
            rews.append(rew)

    log.info(
        f"Parameter eval in model {np.round(k_param, 3)} achieved r {np.round(np.mean(rews), 3)}") #, var {np.round(np.std(rews), 3)}")
    return -np.mean(rews)



def eval_cp(parameters):
    k_param = [parameters["k1"], parameters["k2"], parameters["k3"], parameters["k4"]]
    # These values are replaced and don't matter
    m_c = 1
    m_p = 1
    m_t = m_c + m_p
    g = 9.8
    l = .01
    A = np.array([
        [0, 1, 0, 0],
        [0, g * m_p / m_c, 0, 0],
        [0, 0, 0, 1],
        [0, 0, g * m_t / (l * m_c), 0],
    ])
    B = np.array([
        [0, 1 / m_c, 0, -1 / (l * m_c)],
    ])
    Q = np.diag([.5, .05, 1, .05])
    R = np.ones(1)

    n_dof = np.shape(A)[0]
    modifier = .5 * np.random.random(
        4) + 1  # np.random.random(4)*1.5 # makes LQR values from 0% to 200% of true value
    policy = LQR(A, B.transpose(), Q, R, actionBounds=[-1.0, 1.0])
    policy.K = np.array(k_param)
    env = gym.make("Cartpole-v0")
    s0 = env.reset()
    rews = []
    for i in range(1):
        dotmap = run_controller(env, horizon=200, policy=policy, video=False)
        rews.append(np.sum(dotmap.rewards) / 200)
        dotmap.states = np.stack(dotmap.states)
        dotmap.actions = np.stack(dotmap.actions)
        dotmap.K = np.array(policy.K).flatten()
        exp_data.append(dotmap)
    r = np.mean(rews)
    log.info(
        f"Parameter eval in env {np.round(k_param, 3)} achieved r {np.round(r, 3)}, var {np.round(np.std(rews), 3)}")

    return {"Reward": (np.mean(rews), np.max(np.std(rews)),0.01), }


class CartpoleMetric(Metric):
    def fetch_trial_data(self, trial):
        records = []
        for arm_name, arm in trial.arms_by_name.items():
            params = arm.parameters
            mean, sem = eval_cp(params)
            records.append({
                "arm_name": arm_name,
                "metric_name": self.name,
                "mean": mean,
                "sem": sem,
                "trial_index": trial.index,
            })
        return Data(df=pd.DataFrame.from_records(records))


class CartpoleMetricModel(Metric):
    def fetch_trial_data(self, trial):
        records = []
        for arm_name, arm in trial.arms_by_name.items():
            params = arm.parameters
            mean, sem = eval_cp_model(params)
            records.append({
                "arm_name": arm_name,
                "metric_name": self.name,
                "mean": mean,
                "sem": sem,
                "trial_index": trial.index,
            })
        return Data(df=pd.DataFrame.from_records(records))


@hydra.main(config_path='conf/mbrl.yaml')
def mbrl(cfg):
    # trajectories = torch.load(hydra.utils.get_original_cwd() + '/trajectories/' + label + '/raw' + cfg.data_dir)

    # f = hydra.utils.get_original_cwd() + '/models/' + label + '/'
    # model_one = torch.load(f + cfg.step_model + '.dat')
    # model_traj = torch.load(f + cfg.traj_model + '.dat')

    # get rewards, control policy, etc for each type, and control parameters
    # data_train = trajectories[0]  # [::10] #+trajectories[1]
    # reward = [t['rewards'] for t in data_train]
    # states = [np.float32(t['states']) for t in data_train]
    # actions = [np.float32(t['actions']) for t in data_train]

    # Environment setup
    env_model = cfg.env.name
    label = cfg.env.label
    env = gym.make(env_model)
    env.seed(cfg.random_seed)
    np.random.seed(cfg.random_seed)
    torch.manual_seed(cfg.random_seed)

    global exp_data
    global traj_model
    exp_data = []

    search_space = gen_search_space(cfg.problem)
    if label == "cartpole":
        eval_fn = eval_cp

    # TODO three scenairos
    # 1. BO on the direct env (doable)
    # 2. BO somehow using the traj model to predict reward (can this be a traj model instead of GP?)
    # 3. CMA-ES (or CEM) on traj model to select parameters
    # 4. Make 3 into a loop of some kind

    exp = SimpleExperiment(
        name=cfg.problem.name,
        search_space=SearchSpace(search_space),
        evaluation_function=eval_fn,
        objective_name="Reward",
        # log_scale=True,
        minimize=cfg.metric.minimize,
        # outcome_constraints=outcome_con,
    )

    optimization_config = OptimizationConfig(
        objective=Objective(
            metric=CartpoleMetric(name="Reward"),
            minimize=cfg.metric.minimize,
        ),
    )

    class MyRunner(Runner):
        def run(self, trial):
            return {"name": str(trial.index)}

    exp.runner = MyRunner()
    exp.optimization_config = optimization_config
    from ax.plot.contour import plot_contour

    log.info(f"Running {cfg.bo.random} Sobol initialization trials...")
    sobol = Models.SOBOL(exp.search_space)
    num_search = cfg.bo.random
    for i in range(num_search):
        exp.new_trial(generator_run=sobol.gen(1))
        exp.trials[len(exp.trials) - 1].run()

    def get_data(exper, skip=0):
        raw_data = exper.fetch_data().df.values
        rew = raw_data[:, 2].reshape(-1, 1)
        trials = exper.trials
        params_dict = [trials[i].arm.parameters for i in range(len(trials))]
        params = np.array(np.stack([list(p.values()) for p in params_dict]), dtype=float)
        cat = np.concatenate((rew[skip:], params[skip:, :]), axis=1)
        return cat

    num_opt = cfg.bo.optimized
    # rand_data = get_data(exp)
    sobol_data = exp.eval()

    if cfg.opt == 'bo':
        gpei = Models.BOTORCH(experiment=exp, data=sobol_data)
        for i in range(num_opt):
            # if (i % 5) == 0 and cfg.plot_during:
            #     plot = plot_contour(model=gpei,
            #                         param_x="N",
            #                         param_y="L",
            #                         metric_name="Energy_(uJ)", )
            #     data = plot[0]['data']
            #     lay = plot[0]['layout']
            #
            #     render(plot)

            log.info(f"Running GP+EI optimization trial {i + 1}/{num_opt}...")
            # Reinitialize GP+EI model at each step with updated data.
            batch = exp.new_trial(generator_run=gpei.gen(1))
            gpei = Models.BOTORCH(experiment=exp, data=exp.eval())

        # raw_data = exp.fetch_data().df.values
        # rew = raw_data[:, 2].reshape(-1, 1)
        # trials = exp.trials
        # params_dict = [trials[i].arm.parameters for i in range(len(trials))]
        # params = np.array(np.stack([list(p.values()) for p in params_dict]), dtype=float)
        # cat = np.concatenate((rew, params), axis=1)
        exp_data = get_data(exp)
        sorted_all = exp_data[exp_data[:, 0].argsort()]
        if not cfg.metric.minimize: sorted_all = sorted_all[::-1]  # reverse if minimize

        log.info("10 best param, rewards")
        for i in range(10):
            log.info(
                f"Rew {np.round(sorted_all[i, 0], 4)}, param {np.round(np.array(sorted_all[i, 1:], dtype=float), 3)}")

        log.info(f"Optimal params: {cfg.optimal}")
        plot_learn = plot_learning(exp, cfg)
        # go.Figure(plot_learn).show()
        save_fig([plot_learn], "optimize")

        plot = plot_contour(model=gpei,
                            param_x="k1",
                            param_y="k2",
                            metric_name="Reward",
                            lower_is_better=cfg.metric.minimize)
        save_fig(plot, dir=f"k1k2rew")

        plot = plot_contour(model=gpei,
                            param_x="k3",
                            param_y="k4",
                            metric_name="Reward",
                            lower_is_better=cfg.metric.minimize)
        save_fig(plot, dir=f"k3k4rew")

    elif cfg.opt == 'bo-model':
        from cartpole_lqr import create_dataset_traj
        from dynamics_model import DynamicsModel
        dataset = create_dataset_traj(exp_data, control_params=cfg.model.training.control_params,
                                      train_target=cfg.model.training.train_target,
                                      threshold=cfg.model.training.filter_rate,
                                      t_range=cfg.model.training.t_range)

        traj_model = DynamicsModel(cfg)
        train_logs, test_logs = traj_model.train(dataset, cfg)

        # change to model evaluation!
        exp.evaluation_function = eval_cp_model
        optimization_config_model = OptimizationConfig(
            objective=Objective(
                metric=CartpoleMetricModel(name="Reward"),
                minimize=cfg.metric.minimize,
            ),
        )
        exp.optimization_config = optimization_config_model
        gpei = Models.BOTORCH(experiment=exp, data=sobol_data)
        for i in range(num_opt):
            # if (i % 5) == 0 and cfg.plot_during:
            #     plot = plot_contour(model=gpei,
            #                         param_x="N",
            #                         param_y="L",
            #                         metric_name="Energy_(uJ)", )
            #     data = plot[0]['data']
            #     lay = plot[0]['layout']
            #
            #     render(plot)

            log.info(f"Running GP+EI optimization trial {i + 1}/{num_opt}...")
            # Reinitialize GP+EI model at each step with updated data.
            batch = exp.new_trial(generator_run=gpei.gen(1))
            gpei = Models.BOTORCH(experiment=exp, data=exp.eval())

        # raw_data = exp.fetch_data().df.values
        # rew = raw_data[:, 2].reshape(-1, 1)
        # trials = exp.trials
        # params_dict = [trials[i].arm.parameters for i in range(len(trials))]
        # params = np.array(np.stack([list(p.values()) for p in params_dict]), dtype=float)
        # cat = np.concatenate((rew, params), axis=1)
        exp_data = get_data(exp, skip=cfg.bo.random)
        sorted_all = exp_data[exp_data[:, 0].argsort()]
        if not cfg.metric.minimize: sorted_all = sorted_all[::-1]  # reverse if minimize

        log.info("10 best param, rewards")
        for i in range(10):
            log.info(
                f"Rew {np.round(sorted_all[i, 0], 4)}, param {np.round(np.array(sorted_all[i, 1:], dtype=float), 3)}")

        log.info(f"Optimal params: {cfg.optimal}")
        plot_learn = plot_learning(exp, cfg)
        # go.Figure(plot_learn).show()
        save_fig([plot_learn], "optimize")

        plot = plot_contour(model=gpei,
                            param_x="k1",
                            param_y="k2",
                            metric_name="Reward",
                            lower_is_better=cfg.metric.minimize)
        save_fig(plot, dir=f"k1k2rew")

        plot = plot_contour(model=gpei,
                            param_x="k3",
                            param_y="k4",
                            metric_name="Reward",
                            lower_is_better=cfg.metric.minimize)
        save_fig(plot, dir=f"k3k4rew")
        # raise NotImplementedError("TODO")

    elif cfg.opt == 'cma':
        from cartpole_lqr import create_dataset_traj
        from dynamics_model import DynamicsModel
        dataset = create_dataset_traj(exp_data, control_params=cfg.model.training.control_params,
                                      train_target=cfg.model.training.train_target,
                                      threshold=cfg.model.training.filter_rate,
                                      t_range=cfg.model.training.t_range)
        # global traj_model

        traj_model = DynamicsModel(cfg)
        train_logs, test_logs = traj_model.train(dataset, cfg)

        es = cma.CMAEvolutionStrategy(4 * [0], 1, {'verbose': 1})
        es.optimize(eval_cp_model_scaled)
        # es.result_pretty()
        res = es.result.xbest
        res += [-1, -5, -12, -5]
        res = np.multiply([1, 2, 4, 2], res)
        log.info(f"Final CMA params: {res}")


    else:
        raise NotImplementedError("Other types of opt tbd")


def save_fig(plot, dir):
    plotly.io.orca.config.executable = '/home/hiro/miniconda3/envs/ml_clean/lib/orca_app/orca'
    import plotly.io as pio
    pio.orca.config.use_xvfb = True

    data = plot[0]['data']
    lay = plot[0]['layout']

    fig = {
        "data": data,
        "layout": lay,
    }
    fig = go.Figure(fig)
    fig.update_layout(
        font_family="Times New Roman",
        font_color="Black",
        font_size=14,
        margin=dict(r=5, t=10, l=20, b=20)
    )
    fig.write_image(os.getcwd() + "/" + dir + '.pdf')


def plot_learning(exp, cfg):
    objective_means = np.array([[exp.trials[trial].objective_mean] for trial in exp.trials])
    cumulative = optimization_trace_single_method(
        y=np.maximum.accumulate(objective_means.T, axis=1) * 1.01, ylabel=cfg.metric.name,
        trace_color=tuple((83, 78, 194)),
        # optimum=-3.32237,  # Known minimum objective for Hartmann6 function.
    )
    all = optimization_trace_single_method(
        y=objective_means.T, ylabel=cfg.metric.name,
        model_transitions=[cfg.bo.random], trace_color=tuple((114, 110, 180)),
        # optimum=-3.32237,  # Known minimum objective for Hartmann6 function.
    )

    layout_learn = cumulative[0]['layout']
    layout_learn['paper_bgcolor'] = 'rgba(0,0,0,0)'
    layout_learn['plot_bgcolor'] = 'rgba(0,0,0,0)'
    layout_learn['showlegend'] = False

    d1 = cumulative[0]['data']
    d2 = all[0]['data']

    for t in d1:
        t['legendgroup'] = cfg.metric.name + ", cum. max"
        if 'name' in t and t['name'] == 'Generator change':
            t['name'] = 'End Random Iterations'
        else:
            t['name'] = cfg.metric.name + ", cum. max"
            t['line']['color'] = 'rgba(200,20,20,0.5)'
            t['line']['width'] = 4

    for t in d2:
        t['legendgroup'] = cfg.metric.name
        if 'name' in t and t['name'] == 'Generator change':
            t['name'] = 'End Random Iterations'
        else:
            t['name'] = cfg.metric.name
            t['line']['color'] = 'rgba(20,20,200,0.5)'
            t['line']['width'] = 4

    fig = {
        "data": d1 + d2,  # data,
        "layout": layout_learn,
    }
    return fig


if __name__ == '__main__':
    sys.exit(mbrl())
