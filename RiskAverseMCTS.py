from __future__ import print_function
from common import *
from scipy import optimize
from scipy.stats import rv_discrete
import scipy.linalg as la
import numpy as np
from copy import deepcopy

class RiskAverseMCTS(Agent):
    def __init__(self, mdps, belief, max_depth=1, max_r=1, alpha=1.0, n_iter=200):
        super(RiskAverseMCTS, self).__init__()
        self.mdps = deepcopy(mdps) # identical MDPS, with different transition distributions corresponding to the support of the belief
        self.n_mdps = len(mdps)

        self.orig_belief = belief # save so that we can "reset" agent
        self.belief = np.array(belief) # this one gets updated at every timestep

        self.N_belief_updates = 0

        self.adversarial_belief = np.array(belief) # current best response to the avg performance of the MCTS policy
        self.adversarial_belief_avg = np.array(belief) # avg of past best responses
        self.mixed_strategy_belief = np.array(belief)
        self.adversarial_dist = rv_discrete(values=(np.arange(self.n_mdps), self.mixed_strategy_belief))

        self.gamma = mdps[0].gamma # discount factor extracted from MDP 0

        # The search tree is really just a dictionary, indexed by tuples (s0,a0,s1,a1,...)
        self.Nh = {} # index ends in a state
        self.Nha = {} # index ends in an action
        self.Qha = {} # index ends in an action

        self.model_values = np.zeros(self.n_mdps) # avg performance of the agent under each model

        self.laplace_smoothing = 0
        self.model_counts = self.laplace_smoothing*np.ones(self.n_mdps)

        self.max_depth = max_depth # maximum depth of the search tree

        # Compute the constant factor for the UCB bonus. Should be greater than the max reward possible
        self.max_r = max_r
        self.c = max_r #max_r*max_depth + 0.0000001 # For finite horizon mdps. For infinite horizon, c > max_r/(1-gamma)

        # mixing factors between best response and avg policy.
        self.eta = 0.1 # smoothing of distribution updates
        self.eta_agent = 0.1 # the changes to the UCB policy are fairly smooth, so perhaps this is not necessary / can be higher?

        self.alpha = alpha # CVaR alpha
        self.K = 10 # number of tree updates and model rollouts per adversarial belief update.

        self.n_iter = n_iter # number of adversarial belief updates to run the algorithm for

    # resets the agent to its state when constructed
    def reset(self):
        self.belief = np.array(self.orig_belief)
        self.reset_tree()

    def reset_tree(self):
        self.N_belief_updates = 0
        self.adversarial_belief = np.array(self.orig_belief)
        self.adversarial_dist = rv_discrete(values=(np.arange(self.n_mdps), self.adversarial_belief))
        self.Nh = {}
        self.Nha = {}
        self.Qha = {}
        self.model_values = np.zeros(self.n_mdps)
        self.model_counts = self.laplace_smoothing*np.ones(self.n_mdps)

    # run the search to choose the best action
    def action(self, s):
        self.MCTS(s)
        bestq = -np.inf
        besta = -1
        for a in self.mdps[0].action_space(s):
            qval = self.Qha[(s,a)]
            if qval > bestq:
                bestq = qval
                besta = a

        return besta
        # return self.avg_action((s,))

    # observe a transition, update belief over mdps
    def observe(self, s, a, r, sp):
        self.update_belief(s,a,sp)

    # perform the bayesian belief update (particle filter style)
    def update_belief(self,s,a,sp):
        probs = np.zeros(self.n_mdps)
        for i, mdp in enumerate(self.mdps):
            sp_list, sp_dist = mdp.transition_func(s,a)
            idx = np.nonzero(sp_list == sp)[0][0]
            probs[i] = sp_dist.pmf(idx)

        belief = self.belief*probs
        self.belief = belief/np.sum(belief)
        self.adversarial_belief = deepcopy(self.belief)

    # build the tree from state s
    def MCTS(self, s):
        # reset tree from previous computation?
        self.adv_dists = []
        self.agent_est_value = []
        self.adv_est_value = []

        self.reset_tree()

        for itr in range(self.n_iter):
            for k in range(self.K):
                mdp_i = self.adversarial_dist.rvs() # sample an MDP
                R = self.simulate( (s,), mdp_i, 0 ) # simulate on that MDP, growing the tree in the process

                # TODO: decide whether to update model value estimates while building the tree or separately
                # self.model_counts[mdp_i] += 1
                # self.model_values[mdp_i] += (R - self.model_values[mdp_i])/self.model_counts[mdp_i]

                # collect performance on each mdp purely to update statistics for self.model_values
                for mdp_i in range(self.n_mdps):
                    R = self.simulate( (s,), mdp_i, 0, update_tree=False)
                    self.model_counts[mdp_i] += 1
                    self.model_values[mdp_i] += (R - self.model_values[mdp_i])/self.model_counts[mdp_i]

            # record current statistics for stats purposes
            self.adv_dists.append(deepcopy(self.mixed_strategy_belief))
            value = np.max([self.Qha[(s,a)] for a in self.mdps[0].action_space(s)])
            self.agent_est_value.append(value)
            self.adv_est_value.append(np.dot(self.model_values, self.adversarial_belief_avg))

            self.update_adversarial_belief()


    # solve the LP to adversarially choose a belief within the risk-polytope.
    def update_adversarial_belief(self):
        Aeq = np.ones((1,self.n_mdps))
        beq = 1
        A1 = np.eye(self.n_mdps)
        b1 = self.belief*1/self.alpha
        A2 = -np.eye(self.n_mdps)
        b2 = np.zeros(self.n_mdps)
        A = np.vstack([A1,A2])
        b = np.vstack([b1, b2])

        # do we augment this with "lower confidence bounds?"
        # should we choose adversarial belief assuming the policy will do better or worse than the mean so far?
        # assuming the worst (i.e. optimism wrt the adversary) ensures exploration
        c = np.array(self.model_values)
        for i in range(self.n_mdps):
            if self.model_counts[i] != 0:
                c[i] -= self.c * np.sqrt(np.log(np.sum(self.model_counts))/self.model_counts[i])
            else:
                c[i] = -self.max_r

        # res = belief that minimizes the cost given a lower bound on the expected avg performance of the agent
        res = optimize.linprog(c, A, b, Aeq, beq)

        # set the adversarial belief
        self.adversarial_belief = res.x
        self.N_belief_updates += 1
        self.adversarial_belief_avg += (res.x - self.adversarial_belief_avg)/self.N_belief_updates

        # compute and set the mixed strategy
        self.mixed_strategy_belief = (1-self.eta)*self.adversarial_belief_avg + self.eta*res.x
        self.adversarial_dist = rv_discrete(values=(np.arange(self.n_mdps), self.mixed_strategy_belief))

    # simulate a rollout under mdp_i up to depth, adding a node and updating counts if update_tree=True
    # takes a mixed strategy when choosing actions in the constructed tree
    def simulate(self, h, mdp_i, depth, update_tree=True):
        if depth >= self.max_depth:
            return 0
        if self.mdps[mdp_i].done(h[-1]):
            return 0

        if h not in self.Nh:
            for a in self.mdps[mdp_i].action_space(h[-1]):
                self.Nha[h+(a,)] = 0
                self.Qha[h+(a,)] = 0

            a = self.sample_rollout_action(h)
            r, sp = self.mdps[mdp_i].step(h[-1], a)
            R = r + self.gamma*self.rollout(h + (a,sp), mdp_i, depth + 1)
            if update_tree:
                self.Nh[h] = 1
                self.Nha[h+(a,)] = 1
                self.Qha[h+(a,)] = R
            return R

        # a = self.smooth_ucb_action(h)
        if update_tree:
            a = self.ucb_action(h)
        else:
            a = self.avg_action(h)
        r, sp = self.mdps[mdp_i].step(h[-1], a)

        R = r + self.gamma*self.simulate(h + (a,sp), mdp_i, depth + 1)
        if update_tree:
            self.Nh[h] += 1
            self.Nha[h+(a,)] += 1
            self.Qha[h+(a,)] = self.Qha[h+(a,)] + (R - self.Qha[h+(a,)])*1./self.Nha[h+(a,)]
        return R

    # simulate a rollout from history h in mdp_i up to depth
    # makes no assumptions on h being in the tree
    def rollout(self, h, mdp_i, depth):
        if depth >= self.max_depth:
            return 0
        if self.mdps[mdp_i].done(h[-1]):
            return 0

        a = self.sample_rollout_action(h)
        r, sp = self.mdps[mdp_i].step(h[-1], a)
        return r + self.gamma*self.rollout(h + (a,sp), mdp_i, depth + 1)

    # the policy to use when performing rollouts
    def sample_rollout_action(self, h):
        # randomly sample action
        return np.random.choice(self.mdps[0].action_space(h[-1]))

    # choose an action at history h corresponding to the optimal UCB choice
    def ucb_action(self, h):
        best_a = -1
        best_val = -np.inf
        for a in self.mdps[0].action_space(h[-1]):
            val = np.inf
            if self.Nha[h+(a,)] != 0:
                val = self.Qha[h+(a,)] + self.c * np.sqrt(np.log(self.Nh[h])/self.Nha[h+(a,)])

            if val > best_val:
                best_val = val
                best_a = a
        return best_a

    # choose an action at history h corresponding to the historical distribution of choice
    def avg_action(self, h):
        actions = self.mdps[0].action_space(h[-1])
        if h not in self.Nh:
            probs = np.ones(len(actions))
        else:
            probs = np.array( [self.Nha[h+(a,)]*1. for a in actions] )
        probs = probs/np.sum(probs)
        a = np.random.choice(actions, p=probs)
        return a

    # perform an action at history h corresponding to a mixture of the optimal UCB action and the historical distribution
    def smooth_ucb_action(self, h):
        z = np.random.rand()
        best_a = -1
        if z < self.eta_agent:
            # return the action given by upper confidence bounds
            return self.ucb_action(h)
        else:
            return self.avg_action(h)

        return best_a