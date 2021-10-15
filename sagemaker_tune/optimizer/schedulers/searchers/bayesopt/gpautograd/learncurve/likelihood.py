import autograd.numpy as anp

from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.gpautograd.learncurve.model_params \
    import ISSModelParameters
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.gpautograd.learncurve.posterior_state \
    import GaussProcISSMPosteriorState
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.gpautograd.constants \
    import INITIAL_NOISE_VARIANCE, NOISE_VARIANCE_LOWER_BOUND, \
    NOISE_VARIANCE_UPPER_BOUND, DEFAULT_ENCODING
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.gpautograd.distribution \
    import Gamma
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.gpautograd.gluon \
    import Block
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.gpautograd.gluon_blocks_helpers \
    import encode_unwrap_parameter, register_parameter, create_encoding
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.gpautograd.kernel \
    import KernelFunction
from sagemaker_tune.optimizer.schedulers.searchers.bayesopt.gpautograd.mean \
    import ScalarMeanFunction, MeanFunction


class MarginalLikelihood(Block):
    """
    Marginal likelihood of joint learning curve model, where each curve is
    modelled by a Gaussian ISSM with power-law decay, and values at r_max are
    modelled by a Gaussian process.

    :param kernel: Kernel function k(x, x')
    :param iss_model: ISS model
    :param mean: Mean function mu(x)
    :param initial_noise_variance: A scalar to initialize the value of the
        residual noise variance
    """
    def __init__(
            self, kernel: KernelFunction, iss_model: ISSModelParameters,
            mean: MeanFunction = None, initial_noise_variance=None,
            encoding_type=None, **kwargs):
        super(MarginalLikelihood, self).__init__(**kwargs)
        if mean is None:
            mean = ScalarMeanFunction()
        if initial_noise_variance is None:
            initial_noise_variance = INITIAL_NOISE_VARIANCE
        if encoding_type is None:
            encoding_type = DEFAULT_ENCODING
        self.encoding = create_encoding(
             encoding_type, initial_noise_variance, NOISE_VARIANCE_LOWER_BOUND,
             NOISE_VARIANCE_UPPER_BOUND, 1, Gamma(mean=0.1, alpha=0.1))
        self.mean = mean
        self.kernel = kernel
        self.iss_model = iss_model
        self._components = [
            ('kernel_', self.kernel), ('mean_', self.mean),
            ('issm_', self.iss_model)]
        with self.name_scope():
            self.noise_variance_internal = register_parameter(
                self.params, 'noise_variance', self.encoding)

    def forward(self, data):
        """
        The criterion is the negative log marginal likelihood of `data`, which
        is obtained from `issm.prepare_data`.

        :param data: Input points (features, configs), targets
        """
        state = GaussProcISSMPosteriorState(
            data, self.mean, self.kernel, self.iss_model,
            self.get_noise_variance())
        return state.neg_log_likelihood()

    def param_encoding_pairs(self):
        """
        Return a list of tuples with the Gluon parameters of the likelihood and
        their respective encodings
        """
        own_param_encoding_pairs = [(self.noise_variance_internal,
                                     self.encoding)]
        return own_param_encoding_pairs + self.mean.param_encoding_pairs() + \
               self.kernel.param_encoding_pairs() + \
               self.iss_model.param_encoding_pairs()

    def box_constraints_internal(self):
        """
        Collect the box constraints for all the underlying parameters
        """
        all_box_constraints = {}
        for param, encoding in self.param_encoding_pairs():
            assert encoding is not None,\
                "encoding of param {} should not be None".format(param.name)
            all_box_constraints.update(encoding.box_constraints_internal(param))
        return all_box_constraints

    def get_noise_variance(self, as_ndarray=False):
        noise_variance = encode_unwrap_parameter(
            self.noise_variance_internal, self.encoding)
        return noise_variance if as_ndarray else anp.reshape(noise_variance, (1,))[0]

    def set_noise_variance(self, val):
        self.encoding.set(self.noise_variance_internal, val)
        
    def get_params(self):
        result = {'noise_variance': self.get_noise_variance()}
        for pref, func in self._components:
            result.update({
                (pref + k): v for k, v in func.get_params().items()})
        return result
    
    def set_params(self, param_dict):
        for pref, func in self._components:
            len_pref = len(pref)
            stripped_dict = {
                k[len_pref:]: v for k, v in param_dict.items()
                if k.startswith(pref)}
            func.set_params(stripped_dict)
        self.set_noise_variance(param_dict['noise_variance'])
