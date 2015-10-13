import os, logging, yaml
from collections import OrderedDict
import theano
import theano.tensor as T
from blocks.graph import ComputationGraph
import blocks.graph
from blocks.model import Model
from blocks.filter import VariableFilter
from blocks import roles
from blocks.algorithms import GradientDescent, RMSProp, Adam, CompositeRule, StepClipping
from blocks.main_loop import MainLoop
from blocks.extensions import FinishAfter, Printing, ProgressBar, Timing
from blocks.extensions.stopping import FinishIfNoImprovementAfter
from blocks.extensions.training import SharedVariableModifier, TrackTheBest
from blocks.extensions.monitoring import TrainingDataMonitoring, DataStreamMonitoring
from blocks.extensions.saveload import Checkpoint
import util, attention, crop, tasks, dump
from patchmonitor import PatchMonitoring, VideoPatchMonitoring

floatX = theano.config.floatX

@util.checkargs
def construct_model(patch_shape, hidden_dim, hyperparameters, **kwargs):
    cropper = crop.LocallySoftRectangularCropper(
        name="cropper", kernel=crop.Gaussian(),
        patch_shape=patch_shape, hyperparameters=hyperparameters)
    return attention.RecurrentAttentionModel(
        hidden_dim=hidden_dim, cropper=cropper,
        hyperparameters=hyperparameters,
        # attend based on upper RNN states
        attention_state_name="states#1",
        name="ram")

@util.checkargs
def construct_monitors(algorithm, task, n_patches, x, x_shape, graphs,
                       name, ram, model, n_spatial_dims, plot_url,
                       hyperparameters, patchmonitor_interval=100,
                       **kwargs):
    extensions = []

    if True:
        step_norms = util.Channels()
        step_norms.extend(
            algorithm.steps[param].norm(2).copy(name="%s.step_norm" % name)
            for name, param in model.get_parameter_dict().items())
        step_channels = step_norms.get_channels()

        extensions.append(TrainingDataMonitoring(
            step_channels, prefix="train", after_epoch=True))

    if True:
        data_independent_channels = util.Channels()
        for parameter in graphs["train"].parameters:
            if parameter.name in "gamma beta".split():
                quantity = parameter.mean()
                quantity.name = "%s.mean" % util.get_path(parameter)
                data_independent_channels.append(quantity)
        for key in "location_std scale_std".split():
            data_independent_channels.append(hyperparameters[key].copy(name=key))

        extensions.append(DataStreamMonitoring(
            data_independent_channels.get_channels(),
            data_stream=None, after_epoch=True))

    for which_set in "train valid test".split():
        graph = graphs[which_set]

        channels = util.Channels()
        channels.extend(task.monitor_channels(graph))

        (raw_location, raw_scale,
         true_location, true_scale) = util.get_recurrent_auxiliaries(
             "raw_location raw_scale true_location true_scale".split(),
             graph, n_patches, require_in_graph=True)

        savings, = util.get_recurrent_auxiliaries(
            "savings".split(), graph, n_patches)

        channels.append(savings.mean().copy(name="savings.mean"))

        [excursion_cost] = [var for var in graph.variables
                        if var.name == "excursion_cost"]
        channels.append(excursion_cost)

        for variable_name in "raw_location raw_scale".split():
            variable = locals()[variable_name]
            channels.append(variable.mean(axis=0).T,
                            "%s.mean" % variable_name)
            channels.append(variable.var(axis=0).T,
                            "%s.variance" % variable_name)

        if which_set == "train":
            channels.append(algorithm.total_gradient_norm,
                            "total_gradient_norm")

        extensions.append(DataStreamMonitoring(
            (channels.get_channels() + graph.outputs),
            data_stream=task.get_stream(which_set, monitor=True),
            prefix=which_set, after_epoch=True))

    patchmonitor = None
    if n_spatial_dims == 2:
        patchmonitor_klass = PatchMonitoring
    elif n_spatial_dims == 3:
        patchmonitor_klass = VideoPatchMonitoring

    if patchmonitor_klass:
        patch = T.stack([ram.crop(x, x_shape,
                                  raw_location[:, i, :],
                                  raw_scale[:, i, :])
                         for i in xrange(n_patches)])
        patch = patch.dimshuffle(1, 0, *range(2, patch.ndim))
        patch_extractor = theano.function(
            [x, x_shape], [raw_location, raw_scale, patch])

        for which in "train valid".split():
            patchmonitor = patchmonitor_klass(
                save_to="%s_patches_%s" % (name, which),
                data_stream=task.get_stream(which, shuffle=False, num_examples=10),
                every_n_batches=patchmonitor_interval,
                extractor=patch_extractor,
                map_to_input_space=attention.static_map_to_input_space)
            patchmonitor.save_patches("patchmonitor_test.png")
            extensions.append(patchmonitor)

    if plot_url:
        plot_channels = []
        plot_channels.extend(task.plot_channels())
        plot_channels.append(["train_cost"])
        #plot_channels.append(["train_%s" % step_channel.name for step_channel in step_channels])

        from blocks.extras.extensions.plot import Plot
        extensions.append(Plot(name, channels=plot_channels,
                            after_epoch=True, server_url=plot_url))

    return extensions

@util.checkargs
def get_training_graph(cost, dropout, attention_dropout, recurrent_dropout,
                       recurrent_weight_noise, ram, emitter, **kwargs):
    [cost] = util.replace_by_tags(
        [cost], "location_noise scale_noise".split())
    graph = ComputationGraph(cost)
    if dropout > 0.0:
        graph = emitter.apply_dropout(graph, dropout)
    if recurrent_dropout > 0.0:
        graph = ram.apply_recurrent_dropout(graph, recurrent_dropout)
    if attention_dropout > 0.0:
        graph = ram.apply_attention_dropout(graph, attention_dropout)
    if recurrent_weight_noise > 0.0:
        variables = (VariableFilter(bricks=ram.rnn.children,
                                    roles=[roles.WEIGHT])
                     (graph.parameters))
        assert variables
        graph = blocks.graph.apply_noise(
            graph, variables, recurrent_weight_noise)
    return graph

@util.checkargs
def get_inference_graph(cost, **kwargs):
    return ComputationGraph(cost)

graph_constructors = dict(
    train=get_training_graph,
    valid=get_inference_graph,
    test=get_inference_graph)

@util.checkargs
def construct_main_loop(name, task_name, patch_shape, batch_size,
                        n_spatial_dims, n_patches, max_epochs,
                        patience_epochs, learning_rate,
                        hyperparameters, **kwargs):
    name = "%s_%s" % (name, task_name)
    hyperparameters["name"] = name

    task = tasks.get_task(**hyperparameters)
    hyperparameters["n_channels"] = task.n_channels

    extensions = []

    # let theta noise decay as training progresses
    for key in "location_std scale_std".split():
        hyperparameters[key] = theano.shared(hyperparameters[key], name=key)
        rate = hyperparameters["%s_decay" % key]
        extensions.append(SharedVariableModifier(
            hyperparameters[key],
            lambda i, x: rate * x))

    theano.config.compute_test_value = "warn"

    x, x_shape, y = task.get_variables()

    ram = construct_model(task=task, **hyperparameters)
    ram.initialize()

    states = []
    states.append(ram.compute_initial_state(x, x_shape, as_dict=True))
    n_steps = n_patches - 1
    for i in xrange(n_steps):
        states.append(ram.apply(x, x_shape, as_dict=True, **states[-1]))

    emitter = task.get_emitter(
        input_dim=ram.get_dim("states"),
        **hyperparameters)
    emitter.initialize()
    emitter_cost = emitter.cost(states[-1]["states"], y, n_patches)

    [excursion] = util.get_recurrent_auxiliaries(
        ["excursion"], ComputationGraph(emitter_cost), n_patches)
    excursion_cost = (excursion**2).sum(axis=[1, 2]).mean(axis=0)
    excursion_cost.name = "excursion_cost"

    cost = emitter_cost + excursion_cost
    cost.name = "cost"

    print "setting up main loop..."
    graphs = OrderedDict(
        (which_set, graph_constructors[which_set](
            cost, ram=ram, emitter=emitter, **hyperparameters))
        for which_set in "train valid test".split())

    uselessflunky = Model(graphs["train"].outputs[0])
    algorithm = GradientDescent(
        cost=graphs["train"].outputs[0],
        parameters=graphs["train"].parameters,
        step_rule=CompositeRule([StepClipping(1e2),
                                 Adam(learning_rate=learning_rate)]))
    extensions.extend(construct_monitors(
        x=x, x_shape=x_shape,
        algorithm=algorithm, task=task, model=uselessflunky, ram=ram,
        graphs=graphs, **hyperparameters))
    extensions.extend([
        TrackTheBest("valid_error_rate", "best_valid_error_rate"),
        FinishIfNoImprovementAfter("best_valid_error_rate", epochs=patience_epochs),
        FinishAfter(after_n_epochs=max_epochs),
        dump.DumpBest("best_valid_error_rate", name+"_best.zip"),
        dump.LightCheckpoint(name+"_checkpoint.zip", on_interrupt=False),
        ProgressBar(),
        Timing(),
        Printing(),
        dump.PrintingTo(name+"_log")])
    main_loop = MainLoop(data_stream=task.get_stream("train"),
                         algorithm=algorithm,
                         extensions=extensions,
                         model=uselessflunky)
    return main_loop

if __name__ == "__main__":
    logging.basicConfig()
    logger = logging.getLogger(__name__)

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--hyperparameters", help="YAML file from which to load hyperparameters")
    parser.add_argument("--checkpoint", help="LightCheckpoint zipfile from which to resume training")

    args = parser.parse_args()

    hyperparameters_path = getattr(
        args, "hyperparameters",
        os.path.join(os.path.dirname(__file__), "defaults.yaml"))

    with open(hyperparameters_path, "rb") as f:
        hyperparameters = yaml.load(f)

    hyperparameters["n_spatial_dims"] = len(hyperparameters["patch_shape"])
    hyperparameters["hyperparameters"] = hyperparameters

    main_loop = construct_main_loop(**hyperparameters)

    if args.checkpoint:
        dump.load_main_loop(main_loop, args.checkpoint)

    print "training..."
    main_loop.run()
