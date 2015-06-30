import numpy as np

import theano
import theano.tensor as T

from blocks.initialization import Orthogonal, Constant
from blocks.bricks import MLP, Softmax
from blocks.bricks.cost import CategoricalCrossEntropy, MisclassificationRate

from fuel.datasets.mnist import MNIST
from fuel.streams import DataStream
from fuel.schemes import ShuffledScheme

import util

class Task(object):
    def __init__(self, batch_size, hidden_dim, hyperparameters, shrink_dataset_by=1, **kwargs):
        self.n_classes = 10
        self.n_channels = 1
        hyperparameters["n_channels"] = self.n_channels
        self.datasets = dict(
            train=MNIST(which_sets=["train"], subset=slice(None, 50000)),
            valid=MNIST(which_sets=["train"], subset=slice(50000, None)),
            test=MNIST(which_sets=["test"]))
        self.datastreams = dict(
            (which,
             self.get_stream(which,
                             ShuffledScheme(dataset.num_examples / shrink_dataset_by,
                                            batch_size)))
            for which, dataset in self.datasets.items())
        self.emitter = MLP(activations=[Softmax()],
                           dims=[hidden_dim, self.n_classes],
                           weights_init=Orthogonal(),
                           biases_init=Constant(0))

    def get_stream(self, which_set, scheme):
        return DataStream.default_stream(
            dataset=self.datasets[which_set],
            iteration_scheme=scheme)

    def get_variables(self):
        # shape (batch, channel, height, width)
        x = T.tensor4('features', dtype=theano.config.floatX)
        # shape (batch_size, n_classes)
        y = T.lmatrix('targets')

        theano.config.compute_test_value = 'warn'
        x.tag.test_value = np.random.random((7, self.n_channels, 28, 28)).astype("float32")
        y.tag.test_value = np.random.random_integers(0, 9, (7, 1)).astype("int64")

        return x, y

    def get_emitter(self, hidden_dim, **kwargs):
        return MLP(activations=[Softmax()],
                   dims=[hidden_dim, self.n_classes],
                   weights_init=Orthogonal(),
                   biases_init=Constant(0))

    def compute(self, x, hs, yhats, y):
        yhat = yhats[:, -1, :]
        cross_entropy = util.named(CategoricalCrossEntropy().apply(y.flatten(), yhat),
                                   "cross_entropy")
        error_rate = util.named(MisclassificationRate().apply(y.flatten(), yhat),
                                "error_rate")
        return cross_entropy, [cross_entropy, error_rate]

