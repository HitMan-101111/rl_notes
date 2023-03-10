import gym
import torch as th
import numpy as np
import torch.nn.functional as F

from torch import nn
from torch.optim import Adam
from tqdm import tqdm
from utils import Algorithm, PolicyNet, ValueNet
from utils import get_best_cuda


class PPO(Algorithm):
    def __init__(
        self,
        env: gym.Env,
        gamma: float = 0.99,
        actor_lr: float = 3e-4,
        critic_lr: float = 1e-3,
        batch_size: int = 64,
        latent_dim: int = 64,
        lamda: float = 0.95,
        epsilon: float = 0.2,
        n_epoch: int = 10,
        device: th.device = 'cuda',
        ) -> None:
        self.env = env
        self.batch_size = batch_size
        self.latent_dim = latent_dim
        self.gamma = gamma
        self.lamda = lamda
        self.epsilon = epsilon
        self.n_epoch = n_epoch
        self.device = device
        
        self.policy_net = PolicyNet(self.env, self.device, self.latent_dim)
        self.value_net = ValueNet(self.env, self.device, self.latent_dim)
        
        self.policy_net_optimizer = Adam(self.policy_net.parameters(), lr=actor_lr)
        self.value_net_optimizer = Adam(self.value_net.parameters(), lr=critic_lr)
    
    def take_action(self, observation: np.ndarray, deterministic: bool = False):
        observation = th.as_tensor(observation).float().to(self.device)
        
        probs = self.policy_net(observation)
        action_dist = th.distributions.Categorical(probs)
        if deterministic:
            action = action_dist.mean
        else:
            action = action_dist.sample()
        
        return action.item()

    def train(self, buffer_dict: dict) -> None:
        obs_tensor = th.as_tensor(buffer_dict['observations']).float().to(self.device)
        next_obs_tensor = th.as_tensor(buffer_dict['next_observations']).float().to(self.device)
        action_tensor = th.as_tensor(buffer_dict['actions']).long().to(self.device).view(-1, 1)
        done_tensor = th.as_tensor(buffer_dict['dones']).long().to(self.device).view(-1, 1)
        reward_tensor = th.as_tensor(buffer_dict['rewards']).float().to(self.device).view(-1, 1)
        
        old_logit = self.policy_net(obs_tensor)
        old_log_prob = th.log(th.gather(old_logit, 1, action_tensor)).detach()
        value = self.value_net(obs_tensor)
        next_value = self.value_net(next_obs_tensor) * (1 - done_tensor)
        td_target = reward_tensor + self.gamma * next_value
        td_delta = (td_target - value).detach().cpu().numpy()
        buffer_dict.update({
            'td_delta': td_delta,
        })
        
        self.compute_advantage(buffer_dict)
        advantage_tensor = th.as_tensor(buffer_dict['advantages']).float().to(self.device).view(-1, 1)
        for _ in range(self.n_epoch):
            idx_arr = np.arange(obs_tensor.shape[0])
            np.random.shuffle(idx_arr)
            for sgd_idx in range(0, idx_arr.shape[0], self.batch_size):
                sgd_idx_arr = idx_arr[sgd_idx: sgd_idx + self.batch_size]
                sgd_obs_tensor = obs_tensor[sgd_idx_arr]
                sgd_next_obs_tensor = next_obs_tensor[sgd_idx_arr]
                sgd_action_tensor = action_tensor[sgd_idx_arr]
                sgd_done_tensor = done_tensor[sgd_idx_arr]
                sgd_reward_tensor = reward_tensor[sgd_idx_arr]
                sgd_advantage_tensor = advantage_tensor[sgd_idx_arr]
                sgd_old_log_prob = old_log_prob[sgd_idx_arr]
                
                sgd_logit = self.policy_net(sgd_obs_tensor)
                sgd_log_prob = th.log(th.gather(sgd_logit, 1, sgd_action_tensor))
                ratio = th.exp(sgd_log_prob - sgd_old_log_prob)
                surr0 = ratio * sgd_advantage_tensor
                surr1 = th.clamp(ratio, 1 - self.epsilon, 1 + self.epsilon) * sgd_advantage_tensor
                
                value = self.value_net(sgd_obs_tensor)
                next_value = self.value_net(sgd_next_obs_tensor) * (1 - sgd_done_tensor)
                td_target = sgd_reward_tensor + self.gamma * next_value

                policy_loss = -th.mean(th.min(surr0, surr1))
                value_loss = F.mse_loss(value, td_target)

                self.policy_net_optimizer.zero_grad()
                self.value_net_optimizer.zero_grad()
                policy_loss.backward()
                value_loss.backward()
                self.policy_net_optimizer.step()
                self.value_net_optimizer.step()

    def compute_advantage(self, buffer_dict: dict) -> None:
        done_arr = np.array(buffer_dict['dones'])
        td_delta_arr = np.array(buffer_dict['td_delta'])
        buffer_dict.update({
            'advantages': np.zeros_like(td_delta_arr),
        })
        advantage = 0
        for tau_idx in reversed(range(td_delta_arr.shape[0])):
            if done_arr[tau_idx]:
                advantage = 0
            advantage = advantage + self.gamma * self.lamda * td_delta_arr[tau_idx]
            buffer_dict['advantages'][tau_idx] = advantage
    

if __name__ == '__main__':
    actor_lr = 3e-4
    critic_lr = 1e-2
    num_episodes = 1000
    num_taus = 10
    num_iterations = 10
    batch_size = 64
    latent_dim = 128
    gamma = 0.98
    lamda = 0.95
    epsilon = 0.2
    n_epoch = 10
    log_interval = 10
    seed = 0
    device = f'cuda:{get_best_cuda()}'
    
    env_name = 'CartPole-v0'
    env = gym.make(env_name)
    test_env = gym.make(env_name)
    env.seed(seed)
    test_env.seed(seed)
    th.manual_seed(seed)
    
    agent = PPO(
        env=env,
        actor_lr=actor_lr,
        critic_lr=critic_lr,
        batch_size=batch_size,
        latent_dim=latent_dim,
        gamma=gamma,
        lamda=lamda,
        epsilon=epsilon,
        n_epoch=n_epoch,
        device=device,
    )
    
    returns_list = []
    for iter in range(num_iterations):
        with tqdm(total=int(num_episodes / num_iterations), desc='Iteration %d' % iter) as pbar:
            for i_episode in range(int(num_episodes / num_iterations)):
                episode_returns_sum = 0
                buffer_dict = {
                    'observations': [],
                    'actions': [],
                    'next_observations': [],
                    'rewards': [],
                    'dones': []
                }
                for tau_idx in range(num_taus):
                    observation = env.reset()
                    done = False
                    while not done:
                        action = agent.take_action(observation)
                        next_observation, reward, done, _ = env.step(action)
                        buffer_dict['observations'].append(observation)
                        buffer_dict['actions'].append(action)
                        buffer_dict['next_observations'].append(next_observation)
                        buffer_dict['rewards'].append(reward)
                        buffer_dict['dones'].append(done)
                        observation = next_observation
                        episode_returns_sum += reward
                for key in buffer_dict.keys():
                    buffer_dict[key] = np.array(buffer_dict[key]).astype(float)
                agent.train(buffer_dict)
                returns_list.append(episode_returns_sum / num_taus)
                if (i_episode + 1) % log_interval == 0:
                    pbar.set_postfix({
                        'episode':
                        '%d' % (num_episodes / log_interval * iter + i_episode + 1),
                        'returns':
                        '%.3f' % np.mean(returns_list[-log_interval:])
                    })
                pbar.update(1)

    print(f'Finish training!')
    