##################################################
# @copyright Kandai Watanabe
# @email kandai.wata@gmail.com
# @institute University of Colorado Boulder
#
# NN Models for HIRO
# (Data-Efficient Hierarchical Reinforcement Learning)
# Parameters can be find in the original paper
import os
import copy
import time
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from .nn_rl import Agent, ReplayBuffer
from .utils import get_tensor

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class TD3Actor(nn.Module):
    def __init__(self, state_dim, goal_dim, action_dim, scale):
        super(TD3Actor, self).__init__()
        if scale is None:
            scale = torch.ones(state_dim)
        else:
            scale = get_tensor(scale)
        self.scale = nn.Parameter(scale.clone().detach().float(), requires_grad=False)

        self.l1 = nn.Linear(state_dim + goal_dim, 300)
        self.l2 = nn.Linear(300, 300)
        self.l3 = nn.Linear(300, action_dim)

    def forward(self, state, goal):
        a = F.relu(self.l1(torch.cat([state, goal], 1)))
        a = F.relu(self.l2(a))
        return self.scale * torch.tanh(self.l3(a))

class TD3Critic(nn.Module):
    def __init__(self, state_dim, goal_dim, action_dim):
        super(TD3Critic, self).__init__()
        # Q1
        self.l1 = nn.Linear(state_dim + goal_dim + action_dim, 300)
        self.l2 = nn.Linear(300, 300)
        self.l3 = nn.Linear(300, 1)
        # Q2
        self.l4 = nn.Linear(state_dim + goal_dim + action_dim, 300)
        self.l5 = nn.Linear(300, 300)
        self.l6 = nn.Linear(300, 1)

    def forward(self, state, goal, action):
        sa = torch.cat([state, goal, action], 1)

        q = F.relu(self.l1(sa))
        q = F.relu(self.l2(q))
        q = self.l3(q)

        return q

class TD3():
    def __init__(
        self,
        state_dim,
        goal_dim,
        action_dim,
        scale,
        model_path,
        actor_lr=0.0001,
        critic_lr=0.001,
        policy_noise=0.2,
        noise_clip=0.5,
        gamma=0.99,
        policy_freq=2,
        tau=0.005):

        self.state_dim = state_dim
        self.goal_dim = goal_dim
        self.action_dim = action_dim
        self.model_path = model_path
        self.scale = scale

        self.actor = TD3Actor(state_dim, goal_dim, action_dim, scale=scale).to(device)
        self.actor_target = TD3Actor(state_dim, goal_dim, action_dim, scale=scale).to(device)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=actor_lr)

        self.critic1 = TD3Critic(state_dim, goal_dim, action_dim).to(device)
        self.critic2 = TD3Critic(state_dim, goal_dim, action_dim).to(device)
        self.critic1_target = TD3Critic(state_dim, goal_dim, action_dim).to(device)
        self.critic2_target = TD3Critic(state_dim, goal_dim, action_dim).to(device)
        self.critic1_optimizer = torch.optim.Adam(self.critic1.parameters(), lr=critic_lr, weight_decay=0.0001)
        self.critic2_optimizer = torch.optim.Adam(self.critic2.parameters(), lr=critic_lr, weight_decay=0.0001)

        self.policy_noise = policy_noise
        self.noise_clip = noise_clip
        self.gamma = gamma
        self.policy_freq = policy_freq
        self.tau = tau

        self._initialize_target_networks()
        self.total_it = 0

    def _initialize_target_networks(self):
        self._update_target_network(self.critic1_target, self.critic1, 1.0)
        self._update_target_network(self.critic2_target, self.critic2, 1.0)
        self._update_target_network(self.actor_target, self.actor, 1.0)

    def _update_target_network(self, target, origin, tau):
        for target_param, origin_param in zip(target.parameters(), origin.parameters()):
            target_param.data.copy_(tau * origin_param.data + (1.0 - tau) * target_param.data)

    def save(self):
        if not os.path.exists(os.path.dirname(self.model_path)):
            os.mkdir(os.path.dirname(self.model_path))
        torch.save(self.actor.state_dict(), self.model_path+"_actor")
        torch.save(self.actor_optimizer.state_dict(), self.model_path+"_actor_optimizer")
        torch.save(self.critic1.state_dict(), self.model_path+"_critic1")
        torch.save(self.critic2.state_dict(), self.model_path+"_critic2")
        torch.save(self.critic1_optimizer.state_dict(), self.model_path+"_critic1_optimizer")
        torch.save(self.critic2_optimizer.state_dict(), self.model_path+"_critic2_optimizer")

    def load(self):
        self.actor.load_state_dict(torch.load(self.model_path+"_actor"))
        self.actor_optimizer.load_state_dict(torch.load(self.model_path+"_actor_optimizer"))
        self.critic1.load_state_dict(torch.load(self.model_path+"_critic1"))
        self.critic2.load_state_dict(torch.load(self.model_path+"_critic2"))
        self.critic1_optimizer.load_state_dict(torch.load(self.model_path+"_critic1_optimizer"))
        self.critic2_optimizer.load_state_dict(torch.load(self.model_path+"_critic2_optimizer"))

    # TODO: policy_with_noise
    def policy(self, state, goal, to_numpy=True):
        state = get_tensor(state)
        goal = get_tensor(goal)

        if to_numpy:
            return self.actor(state, goal).cpu().data.numpy().squeeze()
        else:
            return self.actor(state, goal).squeeze()

class HigherController(TD3):
    def __init__(
        self,
        state_dim,
        goal_dim,
        action_dim,
        scale,
        model_path,
        actor_lr=0.0001,
        critic_lr=0.001,
        policy_noise=0.2,
        noise_clip=0.5,
        gamma=0.99,
        policy_freq=2,
        tau=0.005):
        super(HigherController, self).__init__()
        self.model_path = self.model_path + '_high'

    def off_policy_corrections(self, low_con, batch_size, low_goals, states, actions, candidate_goals=8):
        first_s = [s[0] for s in states] # First x
        last_s = [s[-1] for s in states] # Last x

        # Shape: (batchsz, 1, subgoaldim)
        diff_goal = (np.array(last_s) -
                     np.array(first_s))[:, np.newaxis, :self.action_dim]

        # Shape: (batchsz, 1, subgoaldim) #TODO: SCALE!!!!!!!!!!!!!
        original_goal = np.array(low_goals)[:, np.newaxis, :]
        random_goals = np.random.normal(loc=diff_goal, scale=.5*self.scale[None, None, :],
                                        size=(batch_size, candidate_goals, original_goal.shape[-1]))
        random_goals = random_goals.clip(-self.scale, self.scale)

        # Shape: (batchsz, 10, subgoal_dim)
        candidates = np.concatenate([original_goal, diff_goal, random_goals], axis=1)
        states = np.array(states)[:, :-1, :]
        actions = np.array(actions)
        seq_len = len(states[0])

        # For ease
        new_batch_sz = seq_len * batch_size
        action_dim = actions[0][0].shape
        obs_dim = states[0][0].shape
        ncands = candidates.shape[1]

        true_actions = actions.reshape((new_batch_sz,) + action_dim)
        observations = states.reshape((new_batch_sz,) + obs_dim)
        goal_shape = (new_batch_sz, self.action_dim)
        # observations = get_obs_tensor(observations, sg_corrections=True)

        # batched_candidates = np.tile(candidates, [seq_len, 1, 1])
        # batched_candidates = batched_candidates.transpose(1, 0, 2)

        policy_actions = np.zeros((ncands, new_batch_sz) + action_dim)

        # TODO: MULTI_SUBOAL_TRANSITION!!!!!!!!!!!
        for c in range(ncands):
            subgoal = candidates[:,c]
            candidate = (subgoal + states[:, 0, :self.action_dim])[:, None] - states[:, :, :self.action_dim]
            candidate = candidate.reshape(*goal_shape)
            policy_actions[c] = low_con.policy(observations, candidate)

        difference = (policy_actions - true_actions)
        difference = np.where(difference != -np.inf, difference, 0)
        difference = difference.reshape((ncands, batch_size, seq_len) + action_dim).transpose(1, 0, 2, 3)

        logprob = -0.5*np.sum(np.linalg.norm(difference, axis=-1)**2, axis=-1)
        max_indices = np.argmax(logprob, axis=-1)

        return candidates[np.arange(batch_size), max_indices]

    def update(self, replay_buffer, low_con):
        self.total_it += 1

        states, goals, actions, rewards, n_states, done, states_betw, actions_betw = replay_buffer.sample()
        # n_goals = goals
        actions = self.off_policy_corrections(low_con, replay_buffer.batch_size, actions, states_betw, actions_betw)

        with torch.no_grad():
            noise = (
                torch.randn_like(actions) * self.policy_noise
            ).clamp(-self.noise_clip, self.noise_clip)

            n_actions = self.actor_target(n_states, goals) + noise
            n_actions = torch.min(n_actions,  self.actor.scale)
            n_actions = torch.max(n_actions, -self.actor.scale)

            target_Q1 = self.critic1_target(n_states, goals, n_actions)
            target_Q2 = self.critic2_target(n_states, goals, n_actions)
            target_Q = torch.min(target_Q1, target_Q2)
            target_Q = rewards + not_done * self.gamma * target_Q
            target_Q_detached = target_Q.detach()

        current_Q1 = self.critic1(states, goals, actions)
        current_Q2 = self.critic2(states, goals, actions)

        critic1_loss = F.mse_loss(current_Q1, target_Q_detached)
        critic2_loss = F.mse_loss(current_Q2, target_Q_detached)
        critic_loss = critic1_loss + critic2_loss

        self.critic1_optimizer.zero_grad()
        self.critic2_optimizer.zero_grad()
        critic_loss.backward()
        self.critic1_optimizer.step()
        self.critic2_optimizer.step()

        if self.total_it % self.policy_freq == 0:
            a = self.actor(states, goals)
            Q1 = self.critic1(states, goals, a)
            actor_loss = -Q1.mean()

            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            self.actor_optimizer.step()

            self._update_target_network(self.critic1_target, self.critic1, self.tau)
            self._update_target_network(self.critic2_target, self.critic2, self.tau)
            self._update_target_network(self.actor_target, self.actor, self.tau)

    def compute_target_Q(self, actions, goals, n_states)
        with torch.no_grad():
            noise = (
                torch.randn_like(actions) * self.policy_noise
            ).clamp(-self.noise_clip, self.noise_clip)

            n_actions = self.actor_target(n_states, goals) + noise
            n_actions = torch.min(n_actions,  self.actor.scale)
            n_actions = torch.max(n_actions, -self.actor.scale)

            target_Q1 = self.critic1_target(n_states, goals, n_actions)
            target_Q2 = self.critic2_target(n_states, goals, n_actions)
            target_Q = torch.min(target_Q1, target_Q2)
            target_Q = rewards + not_done * self.gamma * target_Q
            target_Q_detached = target_Q.detach()

class LowerController():
    def __init__(
        self,
        state_dim,
        goal_dim,
        action_dim,
        scale,
        model_path,
        actor_lr=0.0001,
        critic_lr=0.001,
        policy_noise=0.2,
        noise_clip=0.5,
        gamma=0.99,
        policy_freq=2,
        tau=0.005):

        self.state_dim = state_dim
        self.action_dim = action_dim
        self.goal_dim = goal_dim
        self.model_path = model_path
        self.policy_noise = policy_noise
        self.noise_clip = noise_clip
        self.gamma = gamma
        self.policy_freq = policy_freq
        self.tau = tau

        self.actor = Actor(state_dim, goal_dim, action_dim, scale=scale).to(device)
        self.actor_target = Actor(state_dim, goal_dim, action_dim, scale=scale).to(device)
        self.actor_target.eval()
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=actor_lr)

        self.critic1 = Critic(state_dim, goal_dim, action_dim).to(device)
        self.critic2= Critic(state_dim, goal_dim, action_dim).to(device)
        self.critic1_target = Critic(state_dim, goal_dim, action_dim).to(device)
        self.critic2_target = Critic(state_dim, goal_dim, action_dim).to(device)
        self.critic1_target.eval()
        self.critic2_target.eval()
        self.critic1_optimizer = torch.optim.Adam(self.critic1.parameters(), lr=critic_lr, weight_decay=0.0001)
        self.critic2_optimizer = torch.optim.Adam(self.critic2.parameters(), lr=critic_lr, weight_decay=0.0001)

        self._initialize_target_networks()

        self.total_it = 0

    def _initialize_target_networks(self):
        self._update_target_network(self.critic1_target, self.critic1, 1.0)
        self._update_target_network(self.critic2_target, self.critic2, 1.0)
        self._update_target_network(self.actor_target, self.actor, 1.0)

    def _update_target_network(self, target, origin, tau):
        for target_param, origin_param in zip(target.parameters(), origin.parameters()):
            target_param.data.copy_(tau * origin_param.data + (1.0 - tau) * target_param.data)

    def save(self):
        if not os.path.exists(os.path.dirname(self.model_path)):
            os.mkdir(os.path.dirname(self.model_path))
        torch.save(self.actor.state_dict(), self.model_path+"_low_actor")
        torch.save(self.actor_optimizer.state_dict(), self.model_path+"_low_actor_optimizer")
        torch.save(self.critic1.state_dict(), self.model_path+"_low_critic1")
        torch.save(self.critic2.state_dict(), self.model_path+"_low_critic2")
        torch.save(self.critic1_optimizer.state_dict(), self.model_path+"_low_critic1_optimizer")
        torch.save(self.critic2_optimizer.state_dict(), self.model_path+"_low_critic2_optimizer")

    def load(self):
        self.critic1.load_state_dict(torch.load(self.model_path+"_low_critic1"))
        self.critic2.load_state_dict(torch.load(self.model_path+"_low_critic2"))
        self.critic1_optimizer.load_state_dict(torch.load(self.model_path+"_low_critic1_optimizer"))
        self.critic2_optimizer.load_state_dict(torch.load(self.model_path+"_low_critic2_optimizer"))
        self.actor.load_state_dict(torch.load(self.model_path+"_low_actor"))
        self.actor_optimizer.load_state_dict(torch.load(self.model_path+"_low_actor_optimizer"))

    def update(self, experiences):
        self.total_it += 1

        # (state, lgoal), a, low_r, (n_s, n_lgoal), float(done)
        states = torch.tensor([e[0] for e in experiences if e is not None], dtype=torch.float32, device=device)
        low_goals = torch.tensor([e[1] for e in experiences if e is not None], dtype=torch.float32, device=device)
        actions = torch.tensor([e[2] for e in experiences if e is not None], dtype=torch.float32, device=device)
        rewards = torch.tensor([e[3] for e in experiences if e is not None], dtype=torch.float32, device=device)
        n_states = torch.tensor([e[4] for e in experiences if e is not None], dtype=torch.float32, device=device)
        n_low_goals = torch.tensor([e[5] for e in experiences if e is not None], dtype=torch.float32, device=device)
        not_done = torch.tensor([1-e[6] for e in experiences if e is not None], dtype=torch.float32, device=device)

        with torch.no_grad():
            noise = (
                torch.randn_like(actions) * self.policy_noise
            ).clamp(-self.noise_clip, self.noise_clip)

            n_actions = self.actor_target(n_states, n_low_goals) + noise
            n_actions = torch.min(n_actions, self.actor.scale)
            n_actions = torch.max(n_actions, -self.actor.scale)

            target_Q1 = self.critic1_target(n_states, n_low_goals, n_actions)
            target_Q2 = self.critic2_target(n_states, n_low_goals, n_actions)
            target_Q = torch.min(target_Q1, target_Q2)
            target_Q = rewards + not_done * self.gamma * target_Q
            target_Q_detached = target_Q.detach()

        current_Q1 = self.critic1(states, low_goals, actions)
        current_Q2 = self.critic2(states, low_goals, actions)

        critic1_loss = F.mse_loss(current_Q1, target_Q_detached)
        critic2_loss = F.mse_loss(current_Q2, target_Q_detached)
        critic_loss = critic1_loss + critic2_loss

        self.critic1_optimizer.zero_grad()
        self.critic2_optimizer.zero_grad()
        critic_loss.backward()
        self.critic1_optimizer.step()
        self.critic2_optimizer.step()

        if self.total_it % self.policy_freq == 0:
            actions = self.actor(states, low_goals)
            Q1 = self.critic1(states, low_goals, actions)
            actor_loss = -Q1.mean()

            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            self.actor_optimizer.step()

            self._update_target_network(self.actor_target, self.actor, self.tau)
            self._update_target_network(self.critic1_target, self.critic1, self.tau)
            self._update_target_network(self.critic2_target, self.critic2, self.tau)

    def policy(self, state, goal, to_numpy=True):
        state = get_tensor(state)
        goal = get_tensor(goal)

        if to_numpy:
            return self.actor(state, goal).cpu().data.numpy().squeeze()
        else:
            return self.actor(state, goal).squeeze()

# relabeling the high-level transition
# Update
# to(device)
# no_grad()
class HiroAgent():
    def __init__(
        self,
        env,
        buffer_size=200000,
        batch_size=100,
        low_buffer_freq=1,
        high_buffer_freq=10, # c steps, not specified in the paper
        low_train_freq=1,
        high_train_freq=10,
        low_sigma=1,
        high_sigma=1,
        c=10,
        manager_reward_scale=0.1,
        model_path='model/hiro_pytorch.h5'):

        self.env = env

        obs = env.reset()
        goal = obs['desired_goal']
        state = obs['observation']
        goal_dim = goal.shape[0]
        state_dim = state.shape[0]
        action_dim = env.action_space.shape[0]
        self.max_action = env.action_space.high

        low = np.array((-10, -10, -0.5, -1, -1, -1, -1,
                -0.5, -0.3, -0.5, -0.3, -0.5, -0.3, -0.5, -0.3))
        high = -low
        man_scale = (high - low)/2
        self.low_goal_dim = man_scale.shape[0]

        self.high_con = HigherController(
            state_dim=state_dim,
            goal_dim=goal_dim,
            action_dim=self.low_goal_dim,
            scale=man_scale,
            model_path=model_path
            )
        self.low_con = LowerController(
            state_dim=state_dim,
            goal_dim=self.low_goal_dim,
            action_dim=action_dim,
            scale=self.max_action,
            model_path=model_path
            )
        self.high_replay_buffer = ReplayBuffer(buffer_size, batch_size)
        self.low_replay_buffer = ReplayBuffer(buffer_size, batch_size)
        self.low_buffer_freq = low_buffer_freq
        self.high_buffer_freq = high_buffer_freq
        self.low_train_freq = low_train_freq
        self.high_train_freq = high_train_freq
        self.low_sigma = low_sigma
        self.high_sigma = high_sigma
        self.c = c
        self.mananger_reward_scale = manager_reward_scale

        self.reward_sum = 0

    def append(self, curr_step, s, low_goal, final_goal, a, n_s, n_low_goal, low_r, high_r, done, info):
        self.reward_sum += high_r

        if curr_step % self.low_buffer_freq == 0:
            # (state, lgoal), a, low_r, (n_s, n_lgoal), float(done)
            self.low_replay_buffer.append([
                s, low_goal,
                a,
                low_r,
                n_s, n_low_goal,
                float(done)
                ])

        if curr_step == 1:
            # state, goal, action, reward, next_state, done, next_states_betw, actions_betw
            self.high_transition = [s, final_goal, low_goal, 0, None, None, [s], []]

        self.high_transition[3] += high_r * self.mananger_reward_scale
        self.high_transition[6].append(n_s)
        self.high_transition[7].append(a)

        if curr_step % self.high_buffer_freq == 0:
            self.high_transition[4] = s
            self.high_transition[5] = float(done)
            self.high_replay_buffer.append(copy.copy(self.high_transition))
            self.high_transition = [s, final_goal, low_goal, 0, None, None, [s], []]

    def train(self, curr_step):
        if curr_step % self.low_train_freq == 0:
            batch = self.low_replay_buffer.sample()
            self.low_con.update(batch)

        if curr_step % self.high_train_freq == 0:
            batch = self.high_replay_buffer.sample()
            self.high_con.update(batch, self.low_con)

        #return  self.low_con.critic_loss.cpu().data.numpy(),    \
        #        self.low_con.actor_loss.cpu().data.numpy(),     \
        #        self.high_con.critic_loss.cpu().data.numpy(),   \
        #        self.high_con.actor_loss.cpu().data.numpy()

    def subgoal_transition(self, s, low_g, n_s):
        return s[:self.low_goal_dim] + low_g - n_s[:self.low_goal_dim]

    def low_reward(self, s, low_g, n_s, scale=1):
        return -np.linalg.norm(s[:2] + low_g[:2] - n_s[:2], 1)*scale

    def augment_with_noise(self, action, sigma):
        aug_action = action + np.random.normal(0, sigma, size=action.shape[0])
        return aug_action.clip(-self.max_action, self.max_action)

    def save(self):
        self.low_con.save()
        self.high_con.save()

    # def load(cls, env, model_path):
    def load(self):
        self.low_con.load()
        self.high_con.load()

    def play(self, episodes=5, render=True, sleep=-1):
        for e in range(episodes):
            obs = self.env.reset()

            final_goal = obs['desired_goal']
            now = obs['achieved_goal']
            s = obs['observation']
            print(final_goal)
            print(now)
            print(s)

            low_goal = self.high_con.policy(s, final_goal)
            done = False
            rewards = 0
            steps = 1

            while not done:
                if render:
                    self.env.render()
                if sleep>0:
                    time.sleep(sleep)
                a = self.low_con.policy(s, low_goal)

                obs, r, done, info = self.env.step(a)
                n_s = obs['observation']

                if steps % self.c == 0:
                    n_low_goal = self.high_con.policy(n_s, final_goal)
                else:
                    n_low_goal = self.subgoal_transition(s, low_goal, n_s)

                rewards += r
                s = n_s
                low_goal = n_low_goal
                steps += 1
            else:
                print("Rewards %.2f"%(rewards/steps))
