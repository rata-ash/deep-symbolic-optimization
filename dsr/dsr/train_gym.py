import os
import sys
import json
import multiprocessing
from itertools import compress
from datetime import datetime
from textwrap import indent

import tensorflow as tf
import pandas as pd
import numpy as np

from dsr.controller import Controller
from dsr.program import Program, from_tokens
from dsr.dataset import Dataset

import gym

# Ignore TensorFlow warnings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
tf.logging.set_verbosity(tf.logging.ERROR)

# Set TensorFlow seed
tf.random.set_random_seed(0)

# Work for multiprocessing pool
def work(p):
    return p.optimize()


def learn(sess, env, controller, logdir=".", n_epochs=1000, batch_size=1,
          reward="neg_mse", reward_params=None, complexity="length",
          complexity_weight=0.001, const_optimizer="minimize",
          const_params=None, alpha=0.1, epsilon=0.01, num_cores=1,
          verbose=True, summary=True, output_file=None, b_jumpstart=True,
          early_stopping=False, threshold=1e-12):
    """
    Executes the main training loop.

    Parameters
    ----------
    sess : tf.Session
        TenorFlow Session object.

    env : gym environment
        The environment we want to find the policy
    
    controller : Controller
        Controller object.
    
    logdir : str, optional
        Name of log directory.
    
    n_epochs : int, optional
        Number of epochs to train.
    
    batch_size : int, optional
        Number of sampled expressions per epoch.
    
    reward : str, optional
        Reward function name.
    
    reward_params : list of str, optional
        List of reward function parameters.
    
    complexity : str, optional
        Complexity penalty name.

    complexity_weight : float, optional
        Coefficient for complexity penalty.

    const_optimizer : str or None, optional
        Name of constant optimizer.
    
    const_params : dict, optional
        Dict of constant optimizer kwargs.
    
    alpha : float, optional
        Coefficient of exponentially-weighted moving average of baseline.
    
    epsilon : float, optional
        Fraction of top expressions used for training.

    num_cores : int, optional
        Number of cores to use for optimizing programs. If -1, uses
        multiprocessing.cpu_count().
    
    verbose : bool, optional
        Whether to print progress.

    summary : bool, optional
        Whether to write TensorFlow summaries.

    output_file : str, optional
        Filename to write results for each iteration.

    b_jumpstart : bool, optional
        Whether baseline start at average reward for the first iteration. If
        False, it starts at 0.0.

    early_stopping : bool, optional
        Whether to stop early if a threshold is reached.

    threshold : float, optional
        NMSE threshold to stop early if a threshold is reached.

    Returns
    -------
    result : dict
        A dict describing the best-fit expression (determined by base_r).
    """

    # Create the summary writer
    if summary:
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        summary_dir = os.path.join("summary", timestamp)
        writer = tf.summary.FileWriter(summary_dir, sess.graph)

    # Create log file
    if output_file is not None:
        logdir = os.path.join("log", logdir)
        os.makedirs(logdir, exist_ok=True)
        output_file = os.path.join(logdir, output_file)
        with open(output_file, 'w') as f:
            # r_best : Maximum across all iterations so far
            # r_max : Maximum across this iteration's batch
            # r_avg_full : Average across this iteration's full batch (before taking epsilon subset)
            # r_avg_sub : Average across this iteration's epsilon-subset batch
            f.write("base_r_best,base_r_max,base_r_avg_full,base_r_avg_sub,r_best,r_max,r_avg_full,r_avg_sub,l_avg_full,l_avg_sub,baseline\n")

    # Set the reward and complexity functions
    reward_params = reward_params if reward_params is not None else []
#    Program.set_reward_function(reward, *reward_params)
    Program.set_complexity_penalty(complexity, complexity_weight)

    # Set the constant optimizer
    const_params = const_params if const_params is not None else {}
    Program.set_const_optimizer(const_optimizer, **const_params)

    # Initialize compute graph
    sess.run(tf.global_variables_initializer())

    # Create the pool of workers
    pool = None
    if "const" in Program.library:
        if num_cores == -1:
            num_cores = multiprocessing.cpu_count()
        if num_cores > 1:
            pool = multiprocessing.Pool(num_cores)

    # Main training loop
    # max_count = 1    
    r_best = -np.inf
    base_r_best = -np.inf
    prev_r_best = None
    prev_base_r_best = None
    b = None if b_jumpstart else 0.0 # Baseline used for control variates
    gym_states =  env.reset()
    for iteration in range(n_epochs): #episodes
#      for step in range(n_epochs):
      env.render()
        # Sample batch of expressions from controller
      actions = controller.sample(batch_size) # Shape: (batch_size, max_length)

      # Instantiate, optimize, and evaluate expressions
      if pool is None:
          programs = [from_tokens(a, optimize=True) for a in actions]
      else:
            # To prevent interfering with the cache, un-optimized programs are
            # first generated serially. The resulting set is optimized in
            # parallel. Since multiprocessing operates on copies of programs,
            # we manually set the optimized constants and base reward after the
            # pool joins.
            programs = [from_tokens(a, optimize=False) for a in actions]
            programs_to_optimize = list(set([p for p in programs if base_r is None]))
            results = pool.map(work, programs_to_optimize)
            for optimized_constants, p in zip(results, programs_to_optimize):
                p.set_constants(optimized_constants)
       
      for step in range(100):

        # In gym environment, consider batch size is always 1
        gym_action = programs[0].execute(np.asarray([gym_states]))
        gym_states, base_reward, done, info =  env.step(gym_action)
        
        # Retrieve the rewards
        base_r =  np.array([base_reward])
        r = np.array([base_r[0] - p.complexity for p in programs])
        l = np.array([len(p.traversal) for p in programs])
        
#        print(r, base_r, l) #[-0.0291273] [-0.0291273] [14]

        
        # Collect full-batch statistics
        base_r_max = np.max(base_r)
        base_r_best = max(base_r_max, base_r_best)
        base_r_avg_full = np.mean(base_r)
        r_max = np.max(r)
        r_best = max(r_max, r_best)
        r_avg_full = np.mean(r)
        l_avg_full = np.mean(l)

        r = np.clip(r, -1e6, np.inf)

        # Compute baseline (EWMA of average reward)
        b = np.mean(r) if b is None else alpha*np.mean(r) + (1 - alpha)*b

        # Collect sub-batch statistics and write output
        if output_file is not None:            
            base_r_avg_sub = np.mean(base_r)
            r_avg_sub = np.mean(r)
            l_avg_sub = np.mean(l)
            stats = np.array([[base_r_best,
                             base_r_max,
                             base_r_avg_full,
                             base_r_avg_sub,
                             r_best,
                             r_max,
                             r_avg_full,
                             r_avg_sub,
                             l_avg_full,
                             l_avg_sub,
                             b]], dtype=np.float32)
            with open(output_file, 'ab') as f:
                np.savetxt(f, stats, delimiter=',')

        # Compute actions mask
        actions_mask = np.zeros_like(actions.T, dtype=np.float32) # Shape: (max_length, batch_size)
        for i,p in enumerate(programs):
            length = min(len(p.traversal), controller.max_length)
            actions_mask[:length, i] = 1.0

        #######################################
        # Train the controller : Thing to do
        # No cutoff in this case --> updated controller. 
        summaries = controller.train_step_gym(r, b, actions, actions_mask)
        if summary:
            writer.add_summary(summaries, step)
            writer.flush()

        # Update new best expression
        new_r_best = False
        new_base_r_best = False
        if prev_r_best is None or r_max > prev_r_best:
            new_r_best = True
            p_r_best = programs[np.argmax(r)]
        if prev_base_r_best is None or base_r_max > prev_base_r_best:
            new_base_r_best = True
            p_base_r_best = programs[np.argmax(base_r)]
        prev_r_best = r_best
        prev_base_r_best = base_r_best

        # Print new best expression
        if verbose:
            if new_r_best and new_base_r_best:
                if p_r_best == p_base_r_best:
                    print("\nNew best overall")
                    p_r_best.print_stats_gym(r,base_r)
                else:
                    print("\nNew best reward")
                    p_r_best.print_stats_gym(r, base_r)
                    print("...and new best base reward")
                    p_base_r_best.print_stats_gym(r, base_r)
            elif new_r_best:
                print("\nNew best reward")
                p_r_best.print_stats_gym(r, base_r)
            elif new_base_r_best:
                print("\nNew best base reward")
                p_base_r_best.print_stats_gym(r, base_r)

        

        # Early stopping
        if early_stopping and base_r > 90:
            print("Iteration "+str(iteration) +"base reward is "+str(base_r)+ " which is above 90; breaking early.")
            #Test result: play episode with learned best equation#
            if iteration > 100:
                print("\n Test result!! \n")
                gym_states =  env.reset()
                for t in range(100):
                     print("\ntest step "+str(t))
                     gym_action = p_r_best.execute(np.asarray([gym_states])) #state-(equ)->action
                     gym_states, base_reward, done, info =  env.step(gym_action) #action-(gym)->reward
                     # Retrieve the rewards
                     base_r_test =  np.array([base_reward])
                     r_test = np.array([base_r_test[0] - p_r_best.complexity])
                     p_r_best.print_stats_gym(r_test, base_r_test)
                print("\n Finish testing !! \n")
            #restart episode for training
            gym_states =  env.reset()
            break


        # print("Step: {}, Loss: {:.6f}, baseline: {:.6f}, r: {:.6f}".format(step, loss, b, np.mean(r)))
        if verbose and step > 0 and step % 10 == 0:
            print("Completed "+str(iteration) +" Iteration : Completed {} steps".format(step))
            #print(" state "+ str(gym_states)+ " reward "+str( base_reward))
            p_r_best.print_stats_gym(r, base_r)

            # print("Neglogp of ground truth action:", controller.neglogp(ground_truth_actions, ground_truth_actions_mask)[0])

    if pool is not None:
        pool.close()

    p = p_base_r_best
    result = {
            "nmse" : 0.0, # dummy
            "r" : r,
            "base_r" : base_r,
            "r_test" : 0.0, #dummy
            "base_r_test" : 0.0, #dummy
            "r_noiseless" : r,
            "base_r_noiseless" : base_r, #there is no noiseless training set
            "r_test_noiseless" : 0.0, #dummy
            "base_r_test_noiseless" : 0.0, #dummy
            "expression" : repr(p.sympy_expr),
            "traversal" : repr(p)
            }
    return result


def main():
    """
    Loads the config file, creates the library and controller, and starts the
    training loop.
    """

    # Load the config file
    config_filename = 'config.json'
    with open(config_filename, encoding='utf-8') as f:
        config = json.load(f)

    config_dataset = config["dataset"]          # Problem specification hyperparameters
    config_training = config["training"]        # Training hyperparameters
    config_controller = config["controller"]    # Controller hyperparameters

    # Define gym environment
    env = gym.envs.make("MountainCarContinuous-v0")

    # Define the dataset and library
    dataset = Dataset(**config_dataset)
    Program.set_training_data(dataset) #dummy line not to break the Program class
    n_input_var = len(env.observation_space.high) # number of states, in this case, 2
    function_set =['add', 'sub', 'mul', 'div', 'sin', 'cos', 'exp', 'log'] #user defined 
    Program.set_library(function_set,n_input_var)

    with tf.Session() as sess:
        # Instantiate the controller
        controller = Controller(sess,  summary=config_training["summary"], **config_controller)
        learn(sess, env, controller, **config_training)


if __name__ == "__main__":
    
        main()