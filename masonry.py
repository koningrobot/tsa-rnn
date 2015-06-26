import operator

import theano.tensor as T

from blocks.bricks.base import lazy, application, Brick
from blocks.bricks.parallel import Fork, Merge
from blocks.bricks.recurrent import BaseRecurrent, recurrent
from blocks.bricks import Linear, Tanh, Rectifier, Initializable, MLP
from blocks.bricks.conv import Flattener

class Merger(Initializable):
    def __init__(self, patch_shape, area_dim, response_dim,
                 area_pretransform=None, response_pretransform=None,
                 area_posttransform=None, response_posttransform=None,
                 **kwargs):
        super(Merger, self).__init__(**kwargs)

        n_spatial_dims = len(patch_shape)
        patch_dim = reduce(operator.mul, patch_shape)

        self.patch_flattener = Flattener()

        self.area = Merge(input_names="location scale".split(),
                          input_dims=[n_spatial_dims, n_spatial_dims],
                          output_dim=area_dim,
                          prototype=area_pretransform)
        self.area.children[0].use_bias = True
        self.area_posttransform = area_posttransform

        self.response = Merge(input_names="area patch".split(),
                              input_dims=[self.area.output_dim, patch_dim],
                              output_dim=response_dim,
                              prototype=response_pretransform)
        self.response.children[0].use_bias = True
        self.response_posttransform = response_posttransform

        self.children = [self.area, self.response, self.patch_flattener,
                         self.area_posttransform, self.response_posttransform]

    @application(inputs="patch location scale".split(),
                 outputs=['response'])
    def apply(self, patch, location, scale):
        flatpatch = self.patch_flattener.apply(patch)
        area = self.area.apply(location, scale)
        area = self.area_posttransform.apply(area)
        response = self.response.apply(area, flatpatch)
        response = self.response_posttransform.apply(response)
        return response

class Locator(Initializable):
    def __init__(self, input_dim, area_dim, n_spatial_dims, area_posttransform=Rectifier(), **kwargs):
        super(Locator, self).__init__(**kwargs)

        self.area = MLP(activations=[area_posttransform], dims=[input_dim, area_dim])
        self.fork = Fork(output_names=['raw_location', 'raw_scale'],
                         input_dim=self.area.output_dim,
                         output_dims=[n_spatial_dims, n_spatial_dims])

        self.children = [self.area, self.fork]

    @application(inputs=['h'], outputs=['location', 'scale'])
    def apply(self, h):
        area = self.area.apply(h)
        raw_location, raw_scale = self.fork.apply(area)
        return raw_location, T.exp(raw_scale)

class SpatialAttention(Initializable):
    def __init__(self, locator, cropper, merger, **kwargs):
        super(SpatialAttention, self).__init__(**kwargs)

        self.locator = locator
        self.cropper = cropper
        self.merger = merger

        self.children = [self.locator, self.cropper, self.merger]

    @application(inputs=['x', 'h'], outputs=['u', 'location', 'scale', 'patch'])
    def apply(self, x, h):
        location, scale = self.locator.apply(h)
        u, patch = self.crop_and_merge(x, location, scale)
        return u, location, scale, patch

    def crop_and_merge(self, x, location, scale):
        patch = self.cropper.apply(x, location, scale)
        u = self.merger.apply(patch, location, scale)
        return u, patch

    @application(inputs=['x'], outputs="u0 location0 scale0 patch0".split())
    def compute_initial_input(self, x):
        location, scale = self.cropper.compute_initial_location_scale(x)
        u, patch = self.crop_and_merge(x, location, scale)
        return u, location, scale, patch

class RecurrentAttentionModel(BaseRecurrent, Initializable):
    def __init__(self, rnn, attention, emitter, **kwargs):
        super(RecurrentAttentionModel, self).__init__(**kwargs)

        self.rnn = rnn
        self.attention = attention
        self.emitter = emitter

        self.children = [self.rnn, self.attention, self.emitter]

    def get_dim(self, name):
        try:
            return dict(h=self.rnn.get_dim("states"))[name]
        except KeyError:
            return super(RecurrentAttentionModel, self).get_dim(name)

    @recurrent(sequences=[''], contexts=['x'], states=['h'], outputs=['yhat', 'h', 'location', 'scale', 'patch'])
    def apply(self, x, h):
        u, location, scale, patch = self.attention.apply(x, h)
        h = self.rnn.apply(states=h, inputs=u, iterate=False)
        yhat = self.emitter.apply(h)
        return yhat, h, location, scale, patch

    @application(inputs=['x'], outputs=['yhat0', 'h0', 'location0', 'scale0', 'patch0'])
    def compute_initial_state(self, x):
        u, location, scale, patch = self.attention.compute_initial_input(x)
        h = self.rnn.apply(states=self.rnn.initial_states(state_name="states", batch_size=x.shape[0]),
                           inputs=u, iterate=False)
        yhat = self.emitter.apply(h)
        return yhat, h, location, scale, patch
