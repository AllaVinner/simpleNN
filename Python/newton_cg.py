import pdb
import tensorflow as tf
import time
import numpy as np
import os
import math
from utilities import predict

def Rop(f, weights, v):
	"""Implementation of R operator
	Args:
		f: any function of weights
		weights: list of tensors.
		v: vector for right multiplication
	Returns:
		Jv: Jaccobian vector product, length same as
			the number of output of f
	"""
	if type(f) == list:
		u = [tf.zeros_like(ff) for ff in f]
	else:
		u = tf.zeros_like(f)  # dummy variable
	g = tf.gradients(ys=f, xs=weights, grad_ys=u)
	return tf.gradients(ys=g, xs=u, grad_ys=v)

def Gauss_Newton_vec(outputs, loss, weights, v):
	"""Implements Gauss-Newton vector product.
	Args:
		loss: Loss function.
		outputs: outputs of the last layer (pre-softmax).
		weights: Weights, list of tensors.
		v: vector to be multiplied with Gauss Newton matrix
	Returns:
		J'BJv: Guass-Newton vector product.
	"""
	# Validate the input
	if type(weights) == list:
		if len(v) != len(weights):
			raise ValueError("weights and v must have the same length.")

	grads_outputs = tf.gradients(ys=loss, xs=outputs)
	BJv = Rop(grads_outputs, weights, v)
	JBJv = tf.gradients(ys=outputs, xs=weights, grad_ys=BJv)
	return JBJv
	

class newton_cg(object):
	def __init__(self, config, sess, outputs, loss):
		"""
		initialize operations and vairables that will be used in newton
		args:
			sess: tensorflow session
			outputs: output of the neural network (pre-softmax layer)
			loss: function to calculate loss
		"""
		super(newton_cg, self).__init__()
		self.sess = sess
		self.config = config
		self.outputs = outputs
		self.loss = loss
		self.param = tf.compat.v1.trainable_variables()

		self.CGiter = 0
		FLOAT = tf.float32
		model_weight = self.vectorize(self.param)
		
		# initial variable used in CG
		zeros = tf.zeros(model_weight.get_shape(), dtype=FLOAT)
		self.r = tf.Variable(zeros, dtype=FLOAT, trainable=False)
		self.v = tf.Variable(zeros, dtype=FLOAT, trainable=False)
		self.s = tf.Variable(zeros, dtype=FLOAT, trainable=False)
		self.g = tf.Variable(zeros, dtype=FLOAT, trainable=False)
		# initial Gv, f for method minibatch
		self.Gv = tf.Variable(zeros, dtype=FLOAT, trainable=False)
		self.f = tf.Variable(0., dtype=FLOAT, trainable=False)

		# rTr, cgtol and beta to be used in CG
		self.rTr = tf.Variable(0., dtype=FLOAT, trainable=False)
		self.cgtol = tf.Variable(0., dtype=FLOAT, trainable=False)
		self.beta = tf.Variable(0., dtype=FLOAT, trainable=False)

		# placeholder alpha, old_alpha and lambda
		self.alpha = tf.compat.v1.placeholder(FLOAT, shape=[])
		self.old_alpha = tf.compat.v1.placeholder(FLOAT, shape=[])
		self._lambda = tf.compat.v1.placeholder(FLOAT, shape=[])

		self.num_grad_segment = math.ceil(self.config.num_data/self.config.bsize)
		self.num_Gv_segment = math.ceil(self.config.GNsize/self.config.bsize)

		cal_loss, cal_lossgrad, cal_lossGv, \
		add_reg_avg_loss, add_reg_avg_grad, add_reg_avg_Gv, \
		zero_loss, zero_grad, zero_Gv = self._ops_in_minibatch()

		# initial operations that will be used in minibatch and newton
		self.cal_loss = cal_loss
		self.cal_lossgrad = cal_lossgrad
		self.cal_lossGv = cal_lossGv
		self.add_reg_avg_loss = add_reg_avg_loss
		self.add_reg_avg_grad = add_reg_avg_grad
		self.add_reg_avg_Gv = add_reg_avg_Gv
		self.zero_loss = zero_loss
		self.zero_grad = zero_grad
		self.zero_Gv = zero_Gv

		self.CG, self.update_v = self._CG()
		self.init_cg_vars = self._init_cg_vars()
		self.update_gs = tf.tensordot(self.s, self.g, axes=1)
		self.update_sGs = 0.5*tf.tensordot(self.s, -self.g-self.r-self._lambda*self.s, axes=1)
		self.update_model = self._update_model()
		self.gnorm = self.calc_norm(self.g)


	def vectorize(self, tensors):
		if isinstance(tensors, list) or isinstance(tensors, tuple):
			vector = [tf.reshape(tensor, [-1]) for tensor in tensors]
			return tf.concat(vector, 0) 
		else:
			return tensors 
	
	def inverse_vectorize(self, vector, param):
		if isinstance(vector, list):
			return vector
		else:
			tensors = []
			offset = 0
			num_total_param = np.sum([np.prod(p.shape.as_list()) for p in param])
			for p in param:
				numel = np.prod(p.shape.as_list())
				tensors.append(tf.reshape(vector[offset: offset+numel], p.shape))
				offset += numel

			assert offset == num_total_param
			return tensors

	def calc_norm(self, v):
		# default: frobenius norm
		if isinstance(v, list):
			norm = 0.
			for p in v:
				norm = norm + tf.norm(tensor=p)**2
			return norm**0.5
		else:
			return tf.norm(tensor=v)

	def _ops_in_minibatch(self):
		"""
		Define operations that will be used in method minibatch

		Vectorization is already a deep copy operation.
		Before using newton method, loss needs to be summed over training samples
		to make results consistent.
		"""

		def cal_loss():
			return tf.compat.v1.assign(self.f, self.f + self.loss)

		def cal_lossgrad():
			update_f = tf.compat.v1.assign(self.f, self.f + self.loss)

			grad = tf.gradients(ys=self.loss, xs=self.param)
			grad = self.vectorize(grad)
			update_grad = tf.compat.v1.assign(self.g, self.g + grad)

			return tf.group(*[update_f, update_grad])

		def cal_lossGv():
			v = self.inverse_vectorize(self.v, self.param)
			Gv = Gauss_Newton_vec(self.outputs, self.loss, self.param, v)
			Gv = self.vectorize(Gv)
			return tf.compat.v1.assign(self.Gv, self.Gv + Gv) 

		# add regularization term to loss, gradient and Gv and further average over batches 
		def add_reg_avg_loss():
			model_weight = self.vectorize(self.param)
			reg = (self.calc_norm(model_weight))**2
			reg = 1.0/(2*self.config.C) * reg
			return tf.compat.v1.assign(self.f, reg + self.f/self.config.num_data)

		def add_reg_avg_lossgrad():
			model_weight = self.vectorize(self.param)
			reg_grad = model_weight/self.config.C
			return tf.compat.v1.assign(self.g, reg_grad + self.g/self.config.num_data)

		def add_reg_avg_lossGv():
			return tf.compat.v1.assign(self.Gv, (self._lambda + 1/self.config.C)*self.v
			 + self.Gv/self.config.GNsize) 

		# zero out loss, grad and Gv 
		def zero_loss():
			return tf.compat.v1.assign(self.f, tf.zeros_like(self.f))
		def zero_grad():
			return tf.compat.v1.assign(self.g, tf.zeros_like(self.g))
		def zero_Gv():
			return tf.compat.v1.assign(self.Gv, tf.zeros_like(self.Gv))

		return (cal_loss(), cal_lossgrad(), cal_lossGv(),
				add_reg_avg_loss(), add_reg_avg_lossgrad(), add_reg_avg_lossGv(),
				zero_loss(), zero_grad(), zero_Gv())

	def minibatch(self, data_batch, place_holder_x, place_holder_y, mode):
		"""
		A function to evaluate either function value, global gradient or sub-sampled Gv
		"""
		if mode not in ('funonly', 'fungrad', 'Gv'):
			raise ValueError('Unknown mode other than funonly & fungrad & Gv!')

		inputs, labels = data_batch
		num_data = labels.shape[0]
		num_segment = math.ceil(num_data/self.config.bsize)
		x, y = place_holder_x, place_holder_y

		# before estimation starts, need to zero out f, grad and Gv according to the mode

		if mode == 'funonly':
			assert num_data == self.config.num_data
			assert num_segment == self.num_grad_segment
			self.sess.run(self.zero_loss)
		elif mode == 'fungrad':
			assert num_data == self.config.num_data
			assert num_segment == self.num_grad_segment
			self.sess.run([self.zero_loss, self.zero_grad])
		else:
			assert num_data == self.config.GNsize
			assert num_segment == self.num_Gv_segment
			self.sess.run(self.zero_Gv)

		for i in range(num_segment):
			
			load_time = time.time()
			idx = np.arange(i * self.config.bsize, min((i+1) * self.config.bsize, num_data))
			batch_input = inputs[idx]
			batch_labels = labels[idx]
			batch_input = np.ascontiguousarray(batch_input)
			batch_labels = np.ascontiguousarray(batch_labels)
			self.config.elapsed_time += time.time() - load_time

			if mode == 'funonly':

				self.sess.run(self.cal_loss, feed_dict={
							x: batch_input, 
							y: batch_labels,})

			elif mode == 'fungrad':
				
				self.sess.run(self.cal_lossgrad, feed_dict={
							x: batch_input, 
							y: batch_labels,})
				
			else:
				
				self.sess.run(self.cal_lossGv, feed_dict={
							x: batch_input, 
							y: batch_labels})

		# average over batches
		if mode == 'funonly':
			self.sess.run(self.add_reg_avg_loss)
		elif mode == 'fungrad':
			self.sess.run([self.add_reg_avg_loss, self.add_reg_avg_grad])
		else:
			self.sess.run(self.add_reg_avg_Gv, 
				feed_dict={self._lambda: self.config._lambda})


	def _update_model(self):
		update_model_ops = []
		x = self.inverse_vectorize(self.s, self.param)
		for i, p in enumerate(self.param):
			op = tf.compat.v1.assign(p, p + (self.alpha-self.old_alpha) * x[i])
			update_model_ops.append(op)
		return tf.group(*update_model_ops)

	def _init_cg_vars(self):
		init_ops = []

		init_r = tf.compat.v1.assign(self.r, -self.g)
		init_v = tf.compat.v1.assign(self.v, -self.g)
		init_s = tf.compat.v1.assign(self.s, tf.zeros_like(self.g))
		gnorm = self.calc_norm(self.g)
		init_rTr = tf.compat.v1.assign(self.rTr, gnorm**2)
		init_cgtol = tf.compat.v1.assign(self.cgtol, self.config.xi*gnorm)

		init_ops = [init_r, init_v, init_s, init_rTr, init_cgtol]

		return tf.group(*init_ops)

	def _CG(self):
		"""
		CG:
			define operations that will be used in method newton

		Same as the previous loss calculation,
		Gv has been summed over batches when samples were fed into Neural Network.
		"""

		def CG_ops():
			
			vGv = tf.tensordot(self.v, self.Gv, axes=1)

			alpha = self.rTr / vGv
			with tf.control_dependencies([alpha]):
				update_s = tf.compat.v1.assign(self.s, self.s + alpha * self.v, name='update_s_ops')
				update_r = tf.compat.v1.assign(self.r, self.r - alpha * self.Gv, name='update_r_ops')

				with tf.control_dependencies([update_s, update_r]):
					rnewTrnew = self.calc_norm(update_r)**2
					update_beta = tf.compat.v1.assign(self.beta, rnewTrnew / self.rTr)
					with tf.control_dependencies([update_beta]):
						update_rTr = tf.compat.v1.assign(self.rTr, rnewTrnew, name='update_rTr_ops')

			return tf.group(*[update_s, update_beta, update_rTr])

		def update_v():
			return tf.compat.v1.assign(self.v, self.r + self.beta*self.v, name='update_v')

		return (CG_ops(), update_v())


	def newton(self, full_batch, val_batch, saver, network, test_network=None):
		"""
		Conduct newton steps for training
		args:
			full_batch & val_batch: provide training set and validation set. The function will
				save the best model evaluted on validation set for future prediction.
			network: a tuple contains (x, y, loss, outputs).
			test_network: a tuple similar to argument network. If you use layers which behave differently
				in test phase such as batchnorm, a separate test_network is needed.
		return:
			None
		"""
		# check whether data is valid
		full_inputs, full_labels = full_batch
		assert full_inputs.shape[0] == full_labels.shape[0]

		if full_inputs.shape[0] != self.config.num_data:
			raise ValueError('The number of full batch inputs does not agree with the config argument.\
							This is important because global loss is averaged over those inputs')

		x, y, _, outputs = network

		tf.compat.v1.summary.scalar('loss', self.f)
		merged = tf.compat.v1.summary.merge_all()
		train_writer = tf.compat.v1.summary.FileWriter('./summary/train', self.sess.graph)

		print(self.config.args)
		if not self.config.screen_log_only:
			log_file = open(self.config.log_file, 'w')
			print(self.config.args, file=log_file)
		
		self.minibatch(full_batch, x, y, mode='fungrad')
		f = self.sess.run(self.f)
		output_str = 'initial f: {:.3f}'.format(f)
		print(output_str)
		if not self.config.screen_log_only:
			print(output_str, file=log_file)
		
		best_acc = 0.0

		total_running_time = 0.0
		self.config.elapsed_time = 0.0
		total_CG = 0
		
		for k in range(self.config.iter_max):

			# randomly select the batch for Gv estimation
			idx = np.random.choice(np.arange(0, full_labels.shape[0]),
					size=self.config.GNsize, replace=False)

			mini_inputs = full_inputs[idx]
			mini_labels = full_labels[idx]

			start = time.time()

			self.sess.run(self.init_cg_vars)
			cgtol = self.sess.run(self.cgtol)

			avg_cg_time = 0.0
			for CGiter in range(1, self.config.CGmax+1):
				
				cg_time = time.time()
				self.minibatch((mini_inputs, mini_labels), x, y, mode='Gv')
				avg_cg_time += time.time() - cg_time
				
				self.sess.run(self.CG)

				rnewTrnew = self.sess.run(self.rTr)
				
				if rnewTrnew**0.5 <= cgtol or CGiter == self.config.CGmax:
					break

				self.sess.run(self.update_v)

			print('Avg time per Gv iteration: {:.5f} s\r\n'.format(avg_cg_time/CGiter))

			gs, sGs = self.sess.run([self.update_gs, self.update_sGs], feed_dict={
					self._lambda: self.config._lambda
				})
			
			# line_search
			f_old = f
			alpha = 1
			while True:

				old_alpha = 0 if alpha == 1 else alpha/0.5
				
				self.sess.run(self.update_model, feed_dict={
					self.alpha:alpha, self.old_alpha:old_alpha
					})

				prered = alpha*gs + (alpha**2)*sGs

				self.minibatch(full_batch, x, y, mode='funonly')
				f = self.sess.run(self.f)

				actred = f - f_old

				if actred <= self.config.eta*alpha*gs:
					break

				alpha *= 0.5

			# update lambda
			ratio = actred / prered
			if ratio < 0.25:
				self.config._lambda *= self.config.boost
			elif ratio >= 0.75:
				self.config._lambda *= self.config.drop

			self.minibatch(full_batch, x, y, mode='fungrad')
			f = self.sess.run(self.f)

			gnorm = self.sess.run(self.gnorm)

			summary = self.sess.run(merged)
			train_writer.add_summary(summary, k)

			# exclude data loading time for fair comparison
			end = time.time() 
			
			end = end - self.config.elapsed_time
			total_running_time += end-start

			self.config.elapsed_time = 0.0
			
			total_CG += CGiter

			output_str = '{}-iter f: {:.3f} |g|: {:.5f} alpha: {:.3e} ratio: {:.3f} lambda: {:.5f} #CG: {} actred: {:.5f} prered: {:.5f} time: {:.3f}'.\
							format(k, f, gnorm, alpha, actred/prered, self.config._lambda, CGiter, actred, prered, end-start)
			print(output_str)
			if not self.config.screen_log_only:
				print(output_str, file=log_file)

			if val_batch is not None:
				# Evaluate the performance after every Newton Step
				if test_network == None:
					val_loss, val_acc, _ = predict(
						self.sess, 
						network=(x, y, self.loss, outputs),
						test_batch=val_batch,
						bsize=self.config.bsize,
						)
				else:
					# A separat test network part has not been done...
					val_loss, val_acc, _ = predict(
						self.sess, 
						network=test_network,
						test_batch=val_batch,
						bsize=self.config.bsize
						)

				output_str = '\r\n {}-iter val_acc: {:.3f}% val_loss {:.3f}\r\n'.\
					format(k, val_acc*100, val_loss)
				print(output_str)
				if not self.config.screen_log_only:
					print(output_str, file=log_file)

				if val_acc > best_acc:
					best_acc = val_acc
					checkpoint_path = self.config.model_file
					save_path = saver.save(self.sess, checkpoint_path)
					print('Best model saved in {}\r\n'.format(save_path))

		if val_batch is None:
			checkpoint_path = self.config.model_file
			save_path = saver.save(self.sess, checkpoint_path)
			print('Model at the last iteration saved in {}\r\n'.format(save_path))
			output_str = 'total_#CG {} | total running time {:.3f}s'.format(total_CG, total_running_time)
		else:
			output_str = 'Final acc: {:.3f}% | best acc {:.3f}% | total_#CG {} | total running time {:.3f}s'.\
				format(val_acc*100, best_acc*100, total_CG, total_running_time)
		print(output_str)
		if not self.config.screen_log_only:
			print(output_str, file=log_file)
			log_file.close()

