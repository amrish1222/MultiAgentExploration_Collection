#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sun Nov  14 09:45:29 2019

@author: bala
"""

import random
import numpy as np
import copy
import math
from sklearn.metrics import mean_squared_error as skMSE

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.nn import init, Parameter
from torch.autograd import Variable
from torch.utils.tensorboard import SummaryWriter

from prioritized_memory import Memory


# Noisy linear layer with independent Gaussian noise
class NoisyLinear(nn.Linear):
  def __init__(self, in_features, out_features, device = "cpu", sigma_init=0.017, bias=True):
    super(NoisyLinear, self).__init__(in_features, out_features, bias=True)  # TODO: Adapt for no bias
    # µ^w and µ^b reuse self.weight and self.bias
    self.sigma_init = sigma_init
    self.sigma_weight = Parameter(torch.Tensor(out_features, in_features))  # σ^w
    self.sigma_bias = Parameter(torch.Tensor(out_features))  # σ^b
    self.register_buffer('epsilon_weight', torch.zeros(out_features, in_features))
    self.register_buffer('epsilon_bias', torch.zeros(out_features))
    self.reset_parameters()
    self.device = device

  def reset_parameters(self):
    if hasattr(self, 'sigma_weight'):  # Only init after all params added (otherwise super().__init__() fails)
      init.uniform(self.weight, -math.sqrt(3 / self.in_features), math.sqrt(3 / self.in_features))
      init.uniform(self.bias, -math.sqrt(3 / self.in_features), math.sqrt(3 / self.in_features))
      init.constant(self.sigma_weight, self.sigma_init)
      init.constant(self.sigma_bias, self.sigma_init)

  def forward(self, input):
    return F.linear(input,
                    self.weight + self.sigma_weight * Variable(self.epsilon_weight),
                    self.bias + self.sigma_bias * Variable(self.epsilon_bias))

  def sample_noise(self):
    self.epsilon_weight = torch.randn(self.out_features, self.in_features).to(self.device)
    self.epsilon_bias = torch.randn(self.out_features).to(self.device)

  def remove_noise(self):
    self.epsilon_weight = torch.zeros(self.out_features, self.in_features)
    self.epsilon_bias = torch.zeros(self.out_features)


class agentModelCNN2_Double_Priority_Noisy(nn.Module):
    def __init__(self,env, device, loggingLevel):
        super().__init__()
        self.stateSpaceSz, \
        self.w, \
        self.h, \
        self.drPos, \
        self.mrVel, \
        self.mrPos, \
        self.dCharge = env.getStateSpace()
        
        self.loggingLevel = loggingLevel
        self.device = device
        
        # cnn
        self.cnn1 = nn.Conv2d(in_channels = 1, out_channels = 16, kernel_size = 5, stride = 2)
        self.cnn2 = nn.Conv2d(in_channels = 16, out_channels = 32, kernel_size = 5, stride = 2)
        self.cnn3 = nn.Conv2d(in_channels = 32, out_channels = 32, kernel_size = 3, stride = 1)
        
        # fc
        self.fcInputs = self.mrPos + self.mrVel + self.drPos + self.dCharge
        self.l1 = nn.Linear(in_features = self.fcInputs, out_features = self.fcInputs)
        
        # concat
        self.fc1 = NoisyLinear(in_features = (6*6*32)+self.fcInputs, out_features = 256, device = self.device)
        self.fc2 = NoisyLinear(in_features = 256, out_features = len(env.getActionSpace()), device = self.device)
    
    def forward(self, x1, x2):
        #logging parameters init
        self.x1_cnn1  = 0
        self.x1_cnn2  = 0
        self.x1_cnn = 0
        #cnn
        if self.loggingLevel == 3:
            self.x1_cnn = x1
            
        x1 = F.relu(self.cnn1(x1))
        x1 = F.relu(self.cnn2(x1))
        if self.loggingLevel == 3:
            self.x1_cnn1 = x1
        x1 = F.relu(self.cnn3(x1))
        if self.loggingLevel == 3:
            self.x1_cnn2 = x1
            
        #fc
        x2 = F.relu(self.l1(x2))
        
        #concat
        x = torch.cat((x1.flatten(start_dim = 1),x2), dim = 1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))

        return x

    def stitch_batch(self,stitched_states):
        # cnn tensor = [num_samples, num_channels, num_width, num_height]
        cnn_x = np.zeros((len(stitched_states),
                          stitched_states[0][0].shape[1],
                          stitched_states[0][0].shape[2],
                          stitched_states[0][0].shape[3]))
        fc_x = []
        for ndx, (cnn_i, fc_i) in enumerate(stitched_states):
            cnn_x[ndx, :, : , :] = cnn_i
            fc_x.append(fc_i)
        cnn_x = torch.from_numpy(cnn_x).to(self.device)
        cnn_x = cnn_x.float()
        fc_x = torch.from_numpy(np.reshape(np.asarray(fc_x),
                                           (len(stitched_states),-1))).to(self.device)
        fc_x = fc_x.float()
        return (cnn_x, fc_x)
    
    def stitch(self,state):
        n_mrPos, \
        n_mrVel, \
        n_localArea, \
        n_dronePos, \
        n_droneVel, \
        n_droneCharge, \
        n_dock, \
        n_reward, \
        n_done = state
        fc_i = np.hstack((np.asarray(n_mrPos).reshape(-1),
                          np.asarray(n_mrVel).reshape(-1),
                          np.asarray(n_dronePos).reshape(-1),
                          np.asarray(n_droneCharge).reshape(-1)))
        n_localArea = np.asarray(n_localArea)
        cnn_i = n_localArea.reshape((1, 1, n_localArea.shape[0], n_localArea.shape[1]))
        return (cnn_i, fc_i)

class DoubleCNNagent_Priority_Noisy():
    def __init__(self,env, loggingLevel):
        self.trainX = []
        self.trainY1 = []
        self.trainY2 = []
        self.replayMemory = []
        self.maxReplayMemory = 20000
        self.memory = Memory(self.maxReplayMemory)
        self.epsilon = 0.0
        self.minEpsilon = 0.01
        self.epsilonDecay = 0.9990
        self.discount = 0.95
        self.learningRate = 0.002
        self.batchSize = 128
        self.envActions = env.getActionSpace()
        self.nActions = len(self.envActions)
        self.loggingLevel = loggingLevel
        self.buildModel(env)
        self.idxs = []
        self.is_weights = []
        self.sw = SummaryWriter(log_dir=f"tf_log/demo_CNN{random.randint(0, 1000)}")
        print(f"Log Dir: {self.sw.log_dir}")
        
    def buildModel(self,env):   
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        print(f'Device : {self.device}')
        self.model1 = agentModelCNN2_Double_Priority_Noisy(env, self.device, self.loggingLevel).to(self.device)
        self.model2 = agentModelCNN2_Double_Priority_Noisy(env, self.device, self.loggingLevel).to(self.device)
        self.loss_fn1 = nn.MSELoss()
        self.loss_fn2 = nn.MSELoss()
        self.optimizer1 = optim.Adam(self.model1.parameters(), lr = self.learningRate)
        self.optimizer2 = optim.Adam(self.model2.parameters(), lr = self.learningRate)
        
    def trainModel(self):
        self.model1.fc1.sample_noise()
        self.model1.fc2.sample_noise()
        
        self.model2.fc1.sample_noise()
        self.model2.fc2.sample_noise()
        
        self.model1.train()
        self.model2.train()
        
        X = self.trainX
        Y1 = torch.from_numpy(self.trainY1).to(self.device)
        Y2 = torch.from_numpy(self.trainY2).to(self.device)
        for i in range(2): # number epoh
            self.optimizer1.zero_grad()
            self.optimizer2.zero_grad()
            predY1 = self.model1(*X)
            predY2 = self.model2(*X)
            loss1 = (torch.FloatTensor(self.is_weights).to(self.device) * F.mse_loss(predY1, Y1)).mean()
            loss2 = (torch.FloatTensor(self.is_weights).to(self.device) * F.mse_loss(predY2, Y2)).mean()
            loss1.backward()
            loss2.backward()
            self.optimizer1.step()
            self.optimizer2.step()
        
    def EpsilonGreedyPolicy(self,state):
        if random.random() <= self.epsilon:
            # choose random
            action = self.envActions[random.randint(0,self.nActions-1)]
        else:
            #ChooseMax
            #Handle multiple max
            self.model1.eval()
            X = self.model1.stitch_batch([self.model1.stitch(state)])
            self.qValues = self.model1(*X).cpu().detach().numpy()[0]
            action = np.random.choice(
                            np.where(self.qValues == np.max(self.qValues))[0]
                            )
        return action
    
    def newGame(self):
        self.trainX = []
        self.trainY = []
    
    def getTrainAction(self,state):
        action = self.EpsilonGreedyPolicy(state)
        return action    
    
    def getAction(self,state):
        self.model1.eval()
        X = self.model1.stitch_batch([self.model1.stitch(state)])
        self.qValues = self.model1(*X).cpu().detach().numpy()[0]
        action = np.random.choice(
                            np.where(self.qValues == np.max(self.qValues))[0]
                            )
        return action
    
    def buildReplayMemory(self, currState, nextState, action):
        self.model1.eval()
        X = self.model1.stitch_batch([self.model1.stitch(currState)])
        q_curr = self.model1(*X).cpu().detach().numpy()[0]
        q_target = copy.deepcopy(q_curr)
        X = self.model1.stitch_batch([self.model1.stitch(nextState)])
        q_next = self.model1(*X).cpu().detach().numpy()[0]
        done = nextState[-1]
        reward = nextState[-2]
        if done:
            q_target[action] = reward
        else:
            q_target[action] = reward + self.discount * np.max(q_next)

        error =  F.mse_loss(torch.FloatTensor(q_curr), 
                            torch.FloatTensor(q_target))
        error = error.numpy()
        self.memory.add(error, (currState, nextState, action))
    
    def buildMiniBatchTrainData(self):
        c = []
        n = []
        r = []
        d = []
        a = []
        if self.memory.tree.n_entries > self.batchSize:
            minibatch_size = self.batchSize
        else:
            minibatch_size = self.memory.tree.n_entries
        
        mini_batch, self.idxs, self.is_weights = self.memory.sample(minibatch_size)
        
        for ndx,[currState, nextState, action] in enumerate(mini_batch):
            c.append(self.model1.stitch(currState))
            n.append(self.model1.stitch(nextState))
            r.append(nextState[-2])
            d.append(nextState[-1])
            a.append([ndx, action])
        c = self.model1.stitch_batch(c)
        n = self.model1.stitch_batch(n)
        r = np.asanyarray(r)
        d = np.asanyarray(d)
        a = np.asanyarray(a)
        a = a.T
        self.model1.eval()
        self.model2.eval()
        X = n
        qVal_n_1 = self.model1(*X).cpu().detach().numpy()
        qVal_n_2 = self.model2(*X).cpu().detach().numpy()
        qMax_n_1 = np.max(qVal_n_1, axis  = 1, keepdims = True)
        qMax_n_2 = np.max(qVal_n_2, axis  = 1, keepdims = True)
        
        qMax_n = np.min(np.hstack((qMax_n_1, qMax_n_2)) , axis = 1)
        X = c
        qVal_c_1 = self.model1(*X).cpu().detach().numpy()
        qVal_c_2 = self.model2(*X).cpu().detach().numpy()
        Y1 = copy.deepcopy(qVal_c_1)
        Y2 = copy.deepcopy(qVal_c_2)
        
        y = np.zeros(r.shape)
        ndx = np.where(d == True)
        y[ndx] = r[ndx]
        ndx = np.where(d == False)
        y[ndx] = r[ndx] + self.discount * qMax_n[ndx]
        Y1[a[0],a[1]] = y
        Y2[a[0],a[1]] = y
        
        self.trainX = c
        self.trainY1 = Y1
        self.trainY2 = Y2

        # update priority
        for ndx, idx in enumerate(self.idxs):
            error = F.mse_loss(torch.FloatTensor(Y1[ndx]),
                            torch.FloatTensor(qVal_c_1[ndx])).numpy()
            self.memory.update(idx, error)

        return (skMSE(Y1,qVal_c_1) + skMSE(Y2,qVal_c_2))/2
        
    def saveModel(self, filePath):
        torch.save(self.model1, f"{filePath}/{self.model1.__class__.__name__}_1.pt")
        torch.save(self.model2, f"{filePath}/{self.model2.__class__.__name__}_2.pt")
    
    def loadModel(self, filePath):
        self.model1 = torch.load(filePath)
        self.model2 = torch.load(filePath)
    
    def summaryWriter_showNetwork(self, curr_state) :
        X = self.model1.stitch_batch([self.model1.stitch(curr_state)])
        self.sw.add_graph(self.model1, X, False)
        self.sw.add_graph(self.model2, X, False)
    
    def summaryWriter_addMetrics(self, episode, loss, reward, lenEpisode):
        self.sw.add_scalar('Loss', loss, episode)
        self.sw.add_scalar('Reward', reward, episode)
        self.sw.add_scalar('Episode Length', lenEpisode, episode)
        self.sw.add_scalar('Epsilon', self.epsilon, episode)
        
        if self.loggingLevel >= 2:
            self.sw.add_histogram('l1_1.bias', self.model1.l1.bias, episode)
            self.sw.add_histogram('l1_1.weight', self.model1.l1.weight, episode)
            self.sw.add_histogram('l1_1.weight.grad', self.model1.l1.weight.grad, episode)
            
            self.sw.add_histogram('l1_2.bias', self.model2.l1.bias, episode)
            self.sw.add_histogram('l1_2.weight', self.model2.l1.weight, episode)
            self.sw.add_histogram('l1_@.weight.grad', self.model2.l1.weight.grad, episode)
        
        if self.loggingLevel >= 3:
            self.sw.add_images("CNN In Model1", self.model1.x1_cnn[0].unsqueeze_(1), dataformats='NCHW', global_step = 5)
            self.sw.add_images("CNN1 Out Model1", self.model1.x1_cnn1[0].unsqueeze_(1), dataformats='NCHW', global_step = 5)
            self.sw.add_images("CNN2 Out Model1", self.model1.x1_cnn2[0].unsqueeze_(1), dataformats='NCHW', global_step = 5)
            
            self.sw.add_images("CNN In Model2", self.model2.x1_cnn[0].unsqueeze_(1), dataformats='NCHW', global_step = 5)
            self.sw.add_images("CNN1 OutModel2", self.model2.x1_cnn1[0].unsqueeze_(1), dataformats='NCHW', global_step = 5)
            self.sw.add_images("CNN2 Out Model2", self.model2.x1_cnn2[0].unsqueeze_(1), dataformats='NCHW', global_step = 5)
    
    def summaryWriter_close(self):
        self.sw.close()