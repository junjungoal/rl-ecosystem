import os, sys

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from models import QNet
from torch.autograd import Variable
from copy import deepcopy
from torch.nn.utils.clip_grad import clip_grad_norm
from tqdm import tqdm
import gc
#from torch.utils.tensorboard import SummaryWriter
import shutil


class DDQN(nn.Module):
    def __init__(self, args, env, q_net, loss_func, opt, lr=0.001,
                 input_dim=55, hidden_dims=[32, 32], action_size=4, agent_emb_dim=5,
                 gamma=0.99):
        super(DDQN, self).__init__()
        self.args = args
        self.env = env
        self.agent_emb_dim = agent_emb_dim
        self.agent_embeddings = {}

        self.num_actions = action_size
        self.loss_func = loss_func
        self.video_flag= True

        self.gamma = gamma

        if torch.cuda.is_available():
            self.dtype = torch.cuda.FloatTensor
            self.dlongtype = torch.cuda.LongTensor
        else:
            self.dtype = torch.FloatTensor
            self.dlongtype = torch.LongTensor

        self.q_net = q_net.type(self.dtype)
        self.opt = opt(self.q_net.parameters(), lr)
        self.target_q_net = deepcopy(q_net).type(self.dtype)

    def train(self,
              episodes=100,
              batch_size=64,
              episode_step=500,
              random_step=1000,
              min_greedy=0.3,
              max_greedy=0.9,
              greedy_step=10000,
              test_step=1000,
              update_period=20,
              train_frequency=4):

        eps_greedy = min_greedy
        g_step = (max_greedy - min_greedy) / greedy_step
        #g_step = 0
        #eps_greedy = 0.9

        rounds = 0
        model_dir = os.path.join('results', 'exp_{:d}'.format(self.args.experiment_num), 'models')
        try:
            os.makedirs(model_dir)
        except:
            shutil.rmtree(model_dir)
            os.makedirs(model_dir)

        for episode in range(episodes):
            loss = 0
            total_reward = 0
            bar = tqdm()

            if episode == 0 or len(self.env.predators) == 0 or len(self.env.preys) == 0 or len(self.env.preys)>10000 or len(self.env.predators)>10000:
                obs = self.env.reset()

                img_dir = os.path.join('results', 'exp_{:d}'.format(self.args.experiment_num), 'images',str(rounds))
                log_dir = os.path.join('results', 'exp_{:d}'.format(self.args.experiment_num), 'logs', str(rounds))
                try:
                    os.makedirs(img_dir)
                except:
                    shutil.rmtree(img_dir)
                    os.makedirs(img_dir)
                try:
                    os.makedirs(log_dir)
                except:
                    shutil.rmtree(log_dir)
                    os.makedirs(log_dir)
                log = open(os.path.join(log_dir, 'log.txt'), 'w')
                rounds += 1
                timesteps = 0

            for i in range(episode_step):
                if self.video_flag:
                    self.env.dump_image(os.path.join(img_dir, '{:d}.png'.format(timesteps+1)))

                eps_greedy += g_step
                eps_greedy = np.clip(eps_greedy, min_greedy, max_greedy)


                actions = []
                ids = []
                action_batches = []
                view_batches = self.env.render()

                for view in view_batches:
                    batch_id, batch_view = self.process_view_with_emb_batch(view)
                    if np.random.rand() < eps_greedy:
                        action = self.q_net(batch_view).max(1)[1].cpu().numpy()
                    else:
                        action = np.random.randint(self.num_actions, size=len(batch_view))
                    ids.extend(batch_id)
                    actions.extend(action)
                    action_batches.append(action)
                actions = dict(zip(ids, actions))
                next_view_batches, rewards = self.env.step(actions)
                total_reward += np.sum(list(rewards.values()))

                loss_batch = 0
                for j, (view, next_view) in enumerate(zip(view_batches, next_view_batches)):
                    view_id, view_values = self.process_view_with_emb_batch(view)
                    next_view_id, next_view_values = self.process_view_with_emb_batch(next_view)
                    #z = self.q_net(Variable(torch.from_numpy(view_values)).type(self.dtype))
                    z = self.q_net(view_values)
                    z = z.gather(1, Variable(torch.Tensor(action_batches[j])).view(len(view_values), 1).type(self.dlongtype))

                    prior_action = self.q_net(next_view_values).max(1)[1].detach()
                    next_q_values = self.target_q_net(next_view_values)
                    next_q_values = next_q_value.gather(1, prior_action.view(-1, 1).type(dlongtype)).view(-1)

                    reward_value = []
                    for id in view_id:
                        if id in rewards:
                            reward_value.append(rewards[id])
                        else:
                            reward_value.append(0.)

                    reward_value = np.array(reward_value)

                    target = Variable(torch.from_numpy(reward_value)).type(self.dtype) + next_q_values * self.gamma
                    target = target.detach().view(len(target), 1) # we do not want to do back-propagation

                    l = self.loss_func(z, target)

                    self.opt.zero_grad()

                    l.backward()
                    #clip_grad_norm(self.q_net.parameters(), 1.)
                    self.opt.step()
                    loss_batch += l.cpu().detach().data.numpy()
                    killed = self.env.remove_dead_agents()
                    self.remove_dead_agent_emb(killed)
                view_batches = next_view_batches
                msg = "episode {:03d} episode step {:03d} reward:{:5.3f} eps_greedy {:5.3f}".format(episode, i, total_reward, eps_greedy)
                bar.set_description(msg)
                bar.update(1)

                info = "Episode\t{:03d}\tStep\t{:03d}\tReward\t{:5.3f}\tnum_agents\t{:d}\tnum_preys\t{:d}\tnum_predators\t{:d}".format(episode, i, total_reward, len(self.env.agents), len(self.env.preys), len(self.env.predators))
                log.write(info+'\n')
                log.flush()
                timesteps += 1

                if i % 5 == 0:
                    self.env.increase_prey(0.006)
                    self.env.increase_predator(0.003)

                if i % update_period:
                    self.update_params()

                if len(self.env.predators) < 1 or len(self.env.preys) < 1 or len(self.env.preys) > 10000 or len(self.env.predators) > 10000:
                    log.close()
                    break


            #images = [os.path.join(img_dir, ("{:d}.png".format(j+1))) for j in range(timesteps)]
            #self.env.make_video(images, outvid=os.path.join(img_dir, 'episode_{:d}.avi'.format(rounds)))
            self.save_model(model_dir, episode)

    def test(self, model_file,
              episodes=100,
              batch_size=64,
              episode_step=200000,
              random_step=1000,
              min_greedy=0.0,
              max_greedy=0.9,
              greedy_step=10000,
              test_step=1000):
        self.q_net = torch.load(model_file)


        total_reward = 0
        bar = tqdm()

        obs = self.env.reset()

        img_dir = os.path.join('results', 'exp_{:d}'.format(self.args.experiment_num), self.args.test_num, 'test_images')
        log_dir = os.path.join('results', 'exp_{:d}'.format(self.args.experiment_num), self.args.test_num, 'test_logs')
        try:
            os.makedirs(img_dir)
        except:
            shutil.rmtree(img_dir)
            os.makedirs(img_dir)
        try:
            os.makedirs(log_dir)
        except:
            shutil.rmtree(log_dir)
            os.makedirs(log_dir)
        log = open(os.path.join(log_dir, 'log.txt'), 'w')

        timesteps = 0

        for i in range(episode_step):
            if self.video_flag:
                self.env.dump_image(os.path.join(img_dir, '{:d}.png'.format(timesteps+1)))

            actions = []
            ids = []
            action_batches = []
            view_batches = self.env.render()

            for view in view_batches:
                batch_id, batch_view = self.process_view_with_emb_batch(view)
                action = self.q_net(batch_view).max(1)[1].cpu().numpy()
                ids.extend(batch_id)
                actions.extend(action)
                action_batches.append(action)
            actions = dict(zip(ids, actions))
            next_view_batches, rewards = self.env.step(actions)
            total_reward += np.sum(list(rewards.values()))
            msg = "episode step {:03d}".format(i)
            bar.set_description(msg)
            bar.update(1)

            info = "Step\t{:03d}\tReward\t{:5.3f}\tnum_agents\t{:d}\tnum_preys\t{:d}\tnum_predators\t{:d}".format(i, total_reward, len(self.env.agents), len(self.env.preys), len(self.env.predators))
            log.write(info+'\n')
            log.flush()
            timesteps += 1
            killed = self.env.remove_dead_agents()
            self.remove_dead_agent_emb(killed)

            if i % 5 == 0:
                self.env.increase_prey(0.006)
                self.env.increase_predator(0.003)


            if len(self.env.predators) < 1 or len(self.env.preys) < 1:
                log.close()
                break
        #images = [os.path.join(img_dir, ("{:d}.png".format(j+1))) for j in range(timesteps)]
        #self.env.make_video(images, outvid=os.path.join(img_dir, 'episode_{:d}.avi')


    def save_model(self, model_dir, episode):
        torch.save(self.q_net, os.path.join(model_dir, "model_{:d}.h5".format(episode)))



    def update_params(self):
        self.target_q_net = deepcopy(self.q_net)

    def take_action(self, state):
        raise NotImplementedError



    def process_view_with_emb_batch(self, input_view):
        batch_id = []
        batch_view = []
        for id, view in input_view:
            if id in self.agent_embeddings:
                new_view = np.concatenate((self.agent_embeddings[id], view), 0)
                batch_view.append(new_view)
                batch_id.append(id)
            else:
                new_embedding = np.random.normal(size=[self.agent_emb_dim])
                self.agent_embeddings[id] = new_embedding
                new_view = np.concatenate((new_embedding, view), 0)
                batch_view.append(new_view)
                batch_id.append(id)
        return batch_id, Variable(torch.from_numpy(np.array(batch_view))).type(self.dtype)

    def remove_dead_agent_emb(self, dead_list):
        for id in dead_list:
            del self.agent_embeddings[id]

