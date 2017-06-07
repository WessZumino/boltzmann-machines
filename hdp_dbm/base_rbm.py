import numpy as np
import tensorflow as tf
from tensorflow.core.framework import summary_pb2

from base import TensorFlowModel, run_in_tf_session
from utils import batch_iter, tbatch_iter, make_inf_generator


class BaseRBM(TensorFlowModel):
    """
    Parameters
    ----------
    learning_rate, momentum : float, iterable, or generator
        Gradient descent parameter
    vb_init : float or iterable
        Visible bias(es).
    metrics_config : dict
        Parameters that controls which metrics and how often they are computed.
        Possible (optional) commands:
        * l2_loss : bool, default False
            Whether to compute weight decay penalty
        * msre : bool, default False
            Whether to compute MSRE = mean squared reconstruction error.
        * pll : bool, default False
            Whether to compute pseudo-loglikelihood estimation. Only makes sense
            to compute for binary visible units (BernoulliRBM, MultinomialRBM).
        * dfe : bool, default False
            Whether to compute delta free energies (free energy gap)
        * l2_loss_fmt : str, default '.2e'
        * msre_fmt : str, default '.4f'
        * pll_fmt : str, default '.3f'
        * dfe_fmt : str, default '.2f'
        * train_metrics_every_iter : int, default 10
        * val_metrics_every_epoch : int, default 1
        * dfe_every_epoch : int, default 2
        * n_batches_for_dfe : int, default 10

    References
    ----------
    [1] Goodfellow I. et. al. "Deep Learning".
    [2] Hinton, G. "A Practical Guide to Training Restricted Boltzmann
        Machines" UTML TR 2010-003
    [3] Restricted Boltzmann Machines (RBMs), Deep Learning Tutorial
        url: http://deeplearning.net/tutorial/rbm.html
    [4] Salakhutdinov, R. and Hinton, G. (2009). Deep Boltzmann machines.
        In AISTATS 2009
    """
    def __init__(self, n_visible=784, n_hidden=256, n_gibbs_steps=1,
                 w_std=0.01, hb_init=0., vb_init=0.,
                 learning_rate=0.01, momentum=0.9, max_epoch=10, batch_size=10, L2=1e-4,
                 metrics_config=None, sample_h_states=False, sample_v_states=False,
                 dbm_first=False, dbm_last=False,
                 verbose=False, model_path='rbm_model/', **kwargs):
        super(BaseRBM, self).__init__(model_path=model_path, **kwargs)
        self.n_visible = n_visible
        self.n_hidden = n_hidden
        self.n_gibbs_steps = n_gibbs_steps

        self.w_std = w_std
        self.hb_init = hb_init

        # Visible biases can be initialized with list of values,
        # because it is often helpful to initialize i-th visible bias
        # with value log(p_i / (1 - p_i)), p_i = fraction of training
        # vectors where i-th unit is on, as proposed in [2]
        self.vb_init = vb_init
        if hasattr(self.vb_init, '__iter__'):
            self._vb_init = self.vb_init = list(self.vb_init)
        else:
            self._vb_init = [self.vb_init] * self.n_visible

        self.learning_rate = learning_rate
        self._learning_rate_gen = None
        self.momentum = momentum
        self._momentum_gen = None
        self.max_epoch = max_epoch
        self.batch_size = batch_size
        self.L2 = L2

        self.metrics_config = metrics_config or {}
        self.metrics_config.setdefault('l2_loss', False)
        self.metrics_config.setdefault('msre', False)
        self.metrics_config.setdefault('pll', False)
        self.metrics_config.setdefault('dfe', False)
        self.metrics_config.setdefault('l2_loss_fmt', '.2e')
        self.metrics_config.setdefault('msre_fmt', '.4f')
        self.metrics_config.setdefault('pll_fmt', '.3f')
        self.metrics_config.setdefault('dfe_fmt', '.2f')
        self.metrics_config.setdefault('train_metrics_every_iter', 10)
        self.metrics_config.setdefault('val_metrics_every_epoch', 1)
        self.metrics_config.setdefault('dfe_every_epoch', 2)
        self.metrics_config.setdefault('n_batches_for_dfe', 10)
        self._train_metrics_names = ('l2_loss', 'msre', 'pll')
        self._train_metrics = {}
        self._val_metrics = {}

        # According to [2], the training goes less noisy and slightly faster, if
        # sampling used for states of hidden units driven by the data, and probabilities
        # for ones driven by reconstructions, and if probabilities (means) used for visible units,
        # both driven by data and by reconstructions. It is therefore recommended to set
        # these parameter to False (default). Note that data driven states for hidden units
        # will be sampled regardless of the provided parameters. `transform` will also use
        # probabilities/means of hidden units.
        self.sample_h_states = sample_h_states
        self.sample_v_states = sample_v_states

        self.verbose = verbose

        # These flags are needed for RBMs which are used for pre-training a DBM
        # to address "double counting evidence" problem [4].
        self.dbm_first = dbm_first
        self.dbm_last = dbm_last

        # current epoch and iteration
        self.epoch = 0
        self.iter = 0

        # input data
        self._X_batch = None
        self._h_rand = None
        self._v_rand = None
        self._pll_rand = None
        self._learning_rate = None
        self._momentum = None

        # weights
        self._W = None
        self._hb = None
        self._vb = None

        # grads
        self._dW = None
        self._dhb = None
        self._dvb = None

        # operations
        self._train_op = None
        self._transform_op = None
        self._msre = None
        self._pll = None
        self._free_energy_op = None

    def _make_placeholders_routine(self, h_rand_shape):
        with tf.name_scope('input_data'):
            self._X_batch = tf.placeholder(tf.float32, [None, self.n_visible], name='X_batch')
            self._h_rand = tf.placeholder(tf.float32, h_rand_shape, name='h_rand')
            self._v_rand = tf.placeholder(tf.float32, [None, self.n_visible], name='v_rand')
            self._pll_rand = tf.placeholder(tf.int32, [None], name='pll_rand')
            self._learning_rate = tf.placeholder(tf.float32, [], name='learning_rate')
            self._momentum = tf.placeholder(tf.float32, [], name='momentum')

    def _make_placeholders(self):
        raise NotImplementedError

    def _make_vars(self):
        with tf.name_scope('weights'):
            W_tensor = tf.random_normal((self.n_visible, self.n_hidden),
                                        mean=0.0, stddev=self.w_std, seed=self.random_seed)
            self._W = tf.Variable(W_tensor, name='W', dtype=tf.float32)
            self._hb = tf.Variable(self.hb_init * tf.ones((self.n_hidden,)), name='hb', dtype=tf.float32)
            self._vb = tf.Variable(self._vb_init, name='vb', dtype=tf.float32)
            tf.summary.histogram('W', self._W)
            tf.summary.histogram('hb', self._hb)
            tf.summary.histogram('vb', self._vb)

        with tf.name_scope('grads'):
            self._dW = tf.Variable(tf.zeros((self.n_visible, self.n_hidden)), name='dW', dtype=tf.float32)
            self._dhb = tf.Variable(tf.zeros((self.n_hidden,)), name='dhb', dtype=tf.float32)
            self._dvb = tf.Variable(tf.zeros((self.n_visible,)), name='dvb', dtype=tf.float32)
            tf.summary.histogram('dW', self._dW)
            tf.summary.histogram('dhb', self._dhb)
            tf.summary.histogram('dvb', self._dvb)

    def _propup(self, v):
        with tf.name_scope('prop_up'):
            t = tf.matmul(v, self._W) + self._hb
            if self.dbm_first: t *= 2.
        return t

    def _propdown(self, h):
        with tf.name_scope('prop_down'):
            t = tf.matmul(a=h, b=self._W, transpose_b=True) + self._vb
            if self.dbm_last: t *= 2.
        return t

    def _h_means_given_v(self, v):
        """Compute means E(h|v)."""
        with tf.name_scope('h_means_given_v'):
            h_means = tf.nn.sigmoid(self._propup(v))
        return h_means

    def _sample_h_given_v(self, h_means):
        """Sample from P(h|v)."""
        with tf.name_scope('sample_h_given_v'):
            h_samples = tf.to_float(tf.less(self._h_rand, h_means))
        return h_samples

    def _v_means_given_h(self, h):
        """Compute means E(v|h)."""
        with tf.name_scope('v_means_given_h'):
            v_means = tf.nn.sigmoid(self._propdown(h))
        return v_means

    def _sample_v_given_h(self, v_means):
        """Sample from P(v|h)."""
        with tf.name_scope('sample_v_given_h'):
            v_samples = tf.to_float(tf.less(self._v_rand, v_means))
        return v_samples

    def _free_energy(self, v):
        """Compute (average) free energy of a visible vectors `v`."""
        raise NotImplementedError

    def _make_train_op(self):
        # Run Gibbs chain for specified number of steps.
        with tf.name_scope('gibbs_chain'):
            h0_means = self._h_means_given_v(self._X_batch)
            h0_samples = self._sample_h_given_v(h0_means)
            h_means, h_samples = None, None
            v_means, v_samples = None, None
            h_states = h0_samples if self.sample_h_states else h0_means
            v_states = None
            for _ in xrange(self.n_gibbs_steps):
                with tf.name_scope('sweep'):
                    v_states = v_means = self._v_means_given_h(h_states)
                    if self.sample_v_states:
                        v_states = self._sample_v_given_h(v_means)
                    h_states = h_means = self._h_means_given_v(v_states)
                    if self.sample_h_states:
                        h_states = self._sample_h_given_v(h_means)

        # encoded data, used by the transform method
        with tf.name_scope('transform_op'):
            transform_op = tf.identity(h_means)
            tf.add_to_collection('transform_op', transform_op)

        # compute gradients estimates (= positive - negative associations)
        with tf.name_scope('grads_estimates'):
            N = tf.to_float(tf.shape(self._X_batch)[0])
            with tf.name_scope('dW'):
                dW_positive = tf.matmul(self._X_batch, h0_means, transpose_a=True)
                dW_negative = tf.matmul(v_states, h_means, transpose_a=True)
                dW = (dW_positive - dW_negative) / N - self.L2 * self._W
            with tf.name_scope('dhb'):
                dhb = tf.reduce_mean(h0_means - h_means, axis=0) # == sum / N
            with tf.name_scope('dvb'):
                dvb = tf.reduce_mean(self._X_batch - v_states, axis=0) # == sum / N

        # update parameters
        with tf.name_scope('momentum_updates'):
            with tf.name_scope('dW'):
                dW_update = self._dW.assign(self._learning_rate * (self._momentum * self._dW + dW))
                W_update = self._W.assign_add(dW_update)
            with tf.name_scope('dhb'):
                dhb_update = self._dhb.assign(self._learning_rate * (self._momentum * self._dhb + dhb))
                hb_update = self._hb.assign_add(dhb_update)
            with tf.name_scope('dvb'):
                dvb_update = self._dvb.assign(self._learning_rate * (self._momentum * self._dvb + dvb))
                vb_update = self._vb.assign_add(dvb_update)

        # assemble train_op
        with tf.name_scope('train_op'):
            train_op = tf.group(W_update, hb_update, vb_update)
            tf.add_to_collection('train_op', train_op)

        # compute metrics
        with tf.name_scope('l2_loss'):
            l2_loss = self.L2 * tf.nn.l2_loss(self._W)
            tf.add_to_collection('l2_loss', l2_loss)

        with tf.name_scope('mean_squared_recon_error'):
            msre = tf.reduce_mean(tf.square(self._X_batch - v_means))
            tf.add_to_collection('msre', msre)

        # Since reconstruction error is fairly poor measure of performance,
        # as this is not what CD-k learning algorithm aims to minimize [2],
        # compute (per sample average) pseudo-loglikelihood (proxy to likelihood)
        # instead, which not only is much more cheaper to compute, but also is
        # an asymptotically consistent estimate of the true log-likelihood [1].
        # More specifically, PLL computed using approximation as in [3].
        with tf.name_scope('pseudo_loglikelihood'):
            x = self._X_batch
            # randomly corrupt one feature in each sample
            x_ = tf.identity(x)
            ind = tf.transpose([tf.range(tf.shape(x)[0]), self._pll_rand])
            m = tf.SparseTensor(indices=tf.to_int64(ind),
                                values=tf.to_float(tf.ones_like(self._pll_rand)),
                                dense_shape=tf.to_int64(tf.shape(x_)))
            x_ = tf.multiply(x_, -tf.sparse_tensor_to_dense(m, default_value=-1))
            x_ = tf.sparse_add(x_, m)

            # TODO: should change to tf.log_sigmoid when updated to r1.2
            pll = -tf.constant(self.n_visible, dtype='float') *\
                             tf.nn.softplus(-(self._free_energy(x_) -
                                              self._free_energy(x)))
            tf.add_to_collection('pll', pll)

        # add also free energy of input batch to collection (for dfe)
        free_energy_op = self._free_energy(self._X_batch)
        tf.add_to_collection('free_energy_op', free_energy_op)

        # collect summaries
        if self.metrics_config['msre']:
            tf.summary.scalar('msre', msre)
        if self.metrics_config['l2_loss']:
            tf.summary.scalar('l2_loss', l2_loss)
        if self.metrics_config['pll']:
            tf.summary.scalar('pll', pll)

    def _make_tf_model(self):
        self._make_placeholders()
        self._make_vars()
        self._make_train_op()

    def _make_h_rand(self, X_batch):
        raise NotImplementedError

    def _make_v_rand(self, X_batch):
        raise NotImplementedError

    def _make_tf_feed_dict(self, X_batch, h_rand=False, v_rand=False, pll_rand=False, training=False):
        feed_dict = {}
        feed_dict['input_data/X_batch:0'] = X_batch
        if h_rand:
            feed_dict['input_data/h_rand:0'] = self._make_h_rand(X_batch)
        if v_rand:
            feed_dict['input_data/v_rand:0'] = self._make_v_rand(X_batch)
        if pll_rand:
            feed_dict['input_data/pll_rand:0'] = self._rng.randint(self.n_visible, size=X_batch.shape[0])
        if training:
            self.learning_rate = next(self._learning_rate_gen)
            feed_dict['input_data/learning_rate:0'] = self.learning_rate
            self.momentum = next(self._momentum_gen)
            feed_dict['input_data/momentum:0'] = self.momentum
        return feed_dict

    def _train_epoch(self, X):
        results = [[] for _ in xrange(len(self._train_metrics))]
        for X_batch in (tbatch_iter if self.verbose else batch_iter)(X, self.batch_size):
            self.iter += 1
            if self.iter % self.metrics_config['train_metrics_every_iter'] == 0:
                run_ops = [v for _, v in sorted(self._train_metrics.items())]
                run_ops += [self._train_op, self._tf_merged_summaries]
                outputs = \
                self._tf_session.run(run_ops,
                                     feed_dict=self._make_tf_feed_dict(X_batch,
                                                                       h_rand=True,
                                                                       v_rand=self.sample_v_states,
                                                                       pll_rand=('pll' in self._train_metrics),
                                                                       training=True))
                values = outputs[:len(self._train_metrics)]
                for i, v in enumerate(values):
                    results[i].append(v)
                train_s = outputs[len(self._train_metrics)]
                self._tf_train_writer.add_summary(train_s, self.iter)
            else:
                self._tf_session.run(self._train_op,
                                     feed_dict=self._make_tf_feed_dict(X_batch,
                                                                       h_rand=True,
                                                                       v_rand=self.sample_v_states,
                                                                       training=True))
        results = map(lambda r: np.mean(r) if r else None, results)
        return dict(zip(sorted(self._train_metrics), results))

    def _run_val_metrics(self, X_val):
        results = [[] for _ in xrange(len(self._val_metrics))]
        for X_vb in batch_iter(X_val, batch_size=self.batch_size):
            run_ops = [v for _, v in sorted(self._val_metrics.items())]
            values = \
            self._tf_session.run(run_ops,
                                 feed_dict=self._make_tf_feed_dict(X_vb,
                                                                   h_rand=True,
                                                                   v_rand=self.sample_v_states,
                                                                   pll_rand=('pll' in self._val_metrics)))
            for i, v in enumerate(values):
                results[i].append(v)
        for i, r in enumerate(results):
            results[i] = np.mean(r) if r else None
        summary_value = []
        for i, m in enumerate(sorted(self._val_metrics)):
            summary_value.append(summary_pb2.Summary.Value(tag=m,
                                                           simple_value=results[i]))
        val_s = summary_pb2.Summary(value=summary_value)
        self._tf_val_writer.add_summary(val_s, self.iter)
        return dict(zip(sorted(self._val_metrics), results))

    def _run_dfe(self, X, X_val):
        """Calculate difference between average free energies of subsets
        of validation and training sets to monitor overfitting,
        as proposed in [2]. If the model is not overfitting at all, this
        quantity should be close to zero. Once this value starts
        growing, the model is overfitting and the value represent the amount
        of overfitting.
        """
        self._free_energy_op = tf.get_collection('free_energy_op')[0]
        train_fes, val_fes = [], []
        for _, X_b in zip(xrange(self.metrics_config['n_batches_for_dfe']),
                          batch_iter(X, batch_size=self.batch_size)):
            train_fe = self._tf_session.run(self._free_energy_op,
                                            feed_dict=self._make_tf_feed_dict(X_b))
            train_fes.append(train_fe)
        for _, X_vb in zip(xrange(self.metrics_config['n_batches_for_dfe']),
                           batch_iter(X_val, batch_size=self.batch_size)):
            val_fe = self._tf_session.run(self._free_energy_op,
                                          feed_dict=self._make_tf_feed_dict(X_vb))
            val_fes.append(val_fe)
        dfe = np.mean(val_fes) - np.mean(train_fes)
        dfe_s = summary_pb2.Summary(value=[summary_pb2.Summary.Value(tag='dfe',
                                                                     simple_value=dfe)])
        self._tf_val_writer.add_summary(dfe_s, self.iter)
        return dfe

    def _fit(self, X, X_val=None):
        # update generators
        self._learning_rate_gen = make_inf_generator(self.learning_rate)
        self._momentum_gen = make_inf_generator(self.momentum)

        # load ops if needed
        self._train_op = tf.get_collection('train_op')[0]
        self._train_metrics = {}
        self._val_metrics = {}
        for m in self._train_metrics_names:
            if self.metrics_config[m]:
                self._train_metrics[m] = tf.get_collection(m)[0]
                if m != 'l2_loss':
                    self._val_metrics[m] = self._train_metrics[m]

        # main loop
        while self.epoch < self.max_epoch:
            val_results = {}
            dfe = None
            self.epoch += 1
            train_results = self._train_epoch(X)
            if X_val is not None and self.epoch % self.metrics_config['val_metrics_every_epoch'] == 0:
                val_results = self._run_val_metrics(X_val)
            if X_val is not None and \
                    self.metrics_config['dfe'] and \
                    self.epoch % self.metrics_config['dfe_every_epoch'] == 0:
                dfe = self._run_dfe(X, X_val)
            if self.verbose:
                s = "epoch: {0:{1}}/{2}".format(self.epoch, len(str(self.max_epoch)), self.max_epoch)
                for m, v in sorted(train_results.items()):
                    if v is not None:
                        s += "; {0}: {1:{2}}".format(m, v, self.metrics_config['{0}_fmt'.format(m)])
                for m, v in sorted(val_results.items()):
                    if v is not None:
                        s += "; val.{0}: {1:{2}}".format(m, v, self.metrics_config['{0}_fmt'.format(m)])
                if dfe is not None: s += " ; dfe: {0:{1}}".format(dfe, self.metrics_config['dfe_fmt'])
                print s
            self._save_model(global_step=self.epoch)

    @run_in_tf_session
    def transform(self, X):
        """Compute hidden units activation probabilities."""
        self._transform_op = tf.get_collection('transform_op')[0]
        H = np.zeros((len(X), self.n_hidden))
        start = 0
        for X_b in batch_iter(X, batch_size=self.batch_size):
            H_b = self._transform_op.eval(feed_dict=self._make_tf_feed_dict(X_b,
                                                                            h_rand=True,
                                                                            v_rand=self.sample_v_states))
            H[start:(start + self.batch_size)] = H_b
            start += self.batch_size
        return H
