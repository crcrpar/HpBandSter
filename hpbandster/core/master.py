import os
import threading
import time
import math
import pdb
import copy
import logging

import numpy as np


from hpbandster.distributed.dispatcher import Dispatcher
from hpbandster.HB_iteration import SuccessiveHalving
from hpbandster.HB_result import HB_result

class HpBandSter(object):
	def __init__(self,
					run_id,
					config_generator,
					working_directory='.',
					ping_interval=60,
					nameserver='127.0.0.1',
					ns_port=None,
					host=None,
					shutdown_workers=True,
					job_queue_sizes=(0,1),
					dynamic_queue_size=False,
					logger=None
					):
		"""

		Parameters
		----------
		run_id : string
			A unique identifier of that Hyperband run. Use the cluster's JobID when running multiple
			concurrent runs to separate them
		config_generator: hpbandster.config_generators object
			An object that can generate new configurations and registers results of executed runs
		working_directory: string
			The top level working directory accessible to all compute nodes(shared filesystem).
		eta : float
			In each iteration, a complete run of sequential halving is executed. In it,
			after evaluating each configuration on the same subset size, only a fraction of
			1/eta of them 'advances' to the next round.
			Must be greater or equal to 2.
		min_budget : float
			The smallest budget to consider. Needs to be positive!
		max_budget : float
			the largest budget to consider. Needs to be larger than min_budget!
			The budgets will be geometrically distributed $\sim \eta^k$ for
			$k\in [0, 1, ... , num_subsets - 1]$.
		ping_interval: int
			number of seconds between pings to discover new nodes. Default is 60 seconds.
		nameserver: str
			address of the Pyro4 nameserver
		ns_port: int
			port of Pyro4 nameserver
		host: str
			ip (or name that resolves to that) of the network interface to use
		shutdown_workers: bool
			flag to control whether the workers are shutdown after the computation is done
		job_queue_size: tuple of ints
			min and max size of the job queue. During the run, when the number of jobs in the queue
			reaches the min value, it will be filled up to the max size. Default: (0,1)
		dynamic_queue_size: bool
			Whether or not to change the queue size based on the number of workers available.
			If true (default), the job_queue_sizes are relative to the current number of workers.

		"""

		self.working_directory = working_directory
		os.makedirs(self.working_directory, exist_ok=True)
		

		if logger is None:
			self.logger = logging.getLogger('hpbandster')
		else:
			self.logger = logger


		self.config_generator = config_generator
		self.time_ref = None


		self.iterations = []
		self.jobs = []

		self.num_running_jobs = 0
		self.job_queue_sizes = job_queue_sizes
		self.user_job_queue_sizes = job_queue_sizes
		self.dynamic_queue_size = dynamic_queue_size

		if job_queue_sizes[0] >= job_queue_sizes[1]:
			raise ValueError("The queue size range needs to be (min, max) with min<max!")


		# condition to synchronize the job_callback and the queue
		self.thread_cond = threading.Condition()

		self.config = {
						'time_ref'   : self.time_ref
					}

		self.dispatcher = Dispatcher( self.job_callback, queue_callback=self.adjust_queue_size, run_id=run_id, ping_interval=ping_interval, nameserver=nameserver, ns_port=ns_port, host=host)

		self.dispatcher_thread = threading.Thread(target=self.dispatcher.run)
		self.dispatcher_thread.start()


	def shutdown(self, shutdown_workers=False):
		self.logger.debug('HBMASTER: shutdown initiated, shutdown_workers = %s'%(str(shutdown_workers)))
		self.dispatcher.shutdown(shutdown_workers)
		self.dispatcher_thread.join()


	def wait_for_workers(min_num_workers=1):
		"""
			helper function to hold execution until some workers are active

			Parameters:
			-----------
			min_n_workers: int
				minimum number of workers present before the run starts		

		"""
		while (self.dispatcher.number_of_workers() < min_n_workers):
			self.logger.debug('HBMASTER: only %i worker(s) available, waiting for at least %i.'%(self.dispatcher.number_of_workers(), min_n_workers))
			time.sleep(1)



	def get_next_iteration(self, iteration):
		"""
			instantiates the next iteration

			Overwrite this to change the iterations for different optimizers

			Parameters:
			-----------
				iteration: int
					the index of the iteration to be instantiated

			Returns:
			--------
				HB_iteration: a valid HB iteration object
		"""
		
		raise NotImplementedError('implement get_next_iteration for %s'%(type(self).__name__))


	def run(self, n_iterations, iteration_class=SuccessiveHalving, min_n_workers=1, iteration_class_kwargs={}):
		"""
			run n_iterations of SuccessiveHalving

			Parameters:
			-----------
			n_iterations: int
				number of iterations to be performed in this run
			iteration_class: SuccessiveHalving like class
				class that runs an iteration of SuccessiveHalving or a similar
				algorithm. The API is defined by the SuccessiveHalving implementation

			iteration_class_kwargs: dict
				Additional keyward arguments passed to iteration_class

		"""


		if self.time_ref is None:
			self.time_ref = time.time()
			self.config['time_ref'] = self.time_ref
		
			self.logger.info('HBMASTER: starting run at %s'%(str(self.time_ref)))



		self.thread_cond.acquire()

		while True:
			
			# find a new run to schedule
			for i in self.active_iterations():
				next_run = self.iterations[i].get_next_run()
				if not next_run is None: break


			if not next_run is None:
				self.logger.debug('HBMASTER: schedule new run for iteration %i'%i)
				self._submit_job(*next_run)
			
			else:						# if no run can be scheduled right now
				if n_iterations > 0:	#we might be able to start the next iteration
					self.iterations.append(get_next_iteration(len(self.iterations)))
					n_iterations -= 1
				else:					#or we have to wait for some job to finish
					self._queue_wait()
				continue				# try again to schedule a new run


			if not self.active_iterations():
				break # current run is finished if there are no active iterations at this point

		self.thread_cond.release()
		return HB_result([copy.deepcopy(i.data) for i in self.iterations], self.config)


	def adjust_queue_size(self, number_of_workers=None):
		if self.dynamic_queue_size:
			self.logger.debug('HBMASTER: adjusting queue size, number of workers %s'%str(number_of_workers))
			with self.thread_cond:
				nw = self.dispatcher.number_of_workers() if number_of_workers is None else number_of_workers
				self.job_queue_sizes = (self.user_job_queue_sizes[0] + nw, self.user_job_queue_sizes[1] + nw)
				self.logger.info('HBMASTER: adjusted queue size to %s'%str(self.job_queue_sizes))
				self.thread_cond.notify_all()


	def job_callback(self, job):
		"""
			method to be called when a job has finished

			this will do some book keeping and call the user defined
			new_result_callback if one was specified
		"""
		self.logger.debug('job_callback for %s'%str(job.id))
		with self.thread_cond:
			self.num_running_jobs -= 1

			if self.num_running_jobs <= self.job_queue_sizes[0]:
				self.logger.debug("HBMASTER: Trying to run another job!")
				self.thread_cond.notify()

			self.iterations[job.id[0]].register_result(job)
		self.config_generator.new_result(job)


	def _queue_wait(self):
		"""
			helper function to wait for the queue to not overflow/underload it
		"""
		
		if self.num_running_jobs >= self.job_queue_sizes[1]:
			while(self.num_running_jobs > self.job_queue_sizes[0]):
				self.logger.debug('HBMASTER: running jobs: %i, queue sizes: %s -> wait'%(self.num_running_jobs, str(self.job_queue_sizes)))
				self.thread_cond.wait()

	def _submit_job(self, config_id, config, budget):
		"""
			hidden function to submit a new job to the dispatcher

			This function handles the actual submission in a
			(hopefully) thread save way
		"""

		with self.thread_cond:
			self.logger.debug('HBMASTER: submitting job %s to dispatcher'%str(config_id))
			job = self.dispatcher.submit_job(config_id, config=config, budget=budget, working_directory=self.working_directory)
			self.num_running_jobs += 1

		#shouldn't the next line be executed while holding the condition?
		self.logger.debug("HBMASTER: job %s submitted to dispatcher"%str(config_id))

	def active_iterations(self):
		""" function to find active (not marked as finished) iterations 

			Returns:
			--------
				list: all active iteration objects (empty if there are none)
		"""

		l = list(filter(lambda idx: not self.iterations[idx].is_finished, range(len(self.iterations))))
		return(l)

	def __del__(self):
		pass
