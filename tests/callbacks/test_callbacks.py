# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

from typing import cast

import pytest

from composer.core import Callback, Engine, Event, State
from composer.core.time import Time
from composer.loggers import Logger, LoggerDestination
from composer.profiler import Profiler, ProfilerAction
from composer.trainer import Trainer
from tests.callbacks.callback_settings import (
    get_cb_kwargs,
    get_cb_model_and_datasets,
    get_cb_patches,
    get_cbs_and_marks,
)
from tests.common import EventCounterCallback


@pytest.fixture
def clean_mlflow_runs():
    """Clean up MLflow runs before and after tests.

    This fixture ensures no MLflow runs persist between tests,
    which prevents "Run already active" errors.
    """
    try:
        import mlflow
        try:
            while mlflow.active_run():
                mlflow.end_run()
        except Exception:
            pass

        yield

        try:
            while mlflow.active_run():
                mlflow.end_run()
        except Exception:
            pass
    except ImportError:
        yield


def test_callbacks_map_to_events():
    # callback methods must be 1:1 mapping with events
    # exception for private methods
    cb = Callback()
    excluded_methods = ['state_dict', 'load_state_dict', 'run_event', 'close', 'post_close']
    methods = {m for m in dir(cb) if (m not in excluded_methods and not m.startswith('_'))}
    event_names = {e.value for e in Event}
    assert methods == event_names


@pytest.mark.parametrize('event', list(Event))
def test_run_event_callbacks(event: Event, dummy_state: State):
    callback = EventCounterCallback()
    logger = Logger(dummy_state)
    dummy_state.callbacks = [callback]
    engine = Engine(state=dummy_state, logger=logger)

    engine.run_event(event)

    assert callback.event_to_num_calls[event] == 1


@pytest.mark.parametrize('cb_cls', get_cbs_and_marks(callbacks=True, loggers=True, profilers=True))
class TestCallbacks:

    @classmethod
    def setup_class(cls):
        pytest.importorskip('wandb', reason='WandB is optional.')

    @pytest.mark.filterwarnings('ignore::UserWarning')
    def test_callback_is_constructable(self, cb_cls: type[Callback]):
        cb_kwargs = get_cb_kwargs(cb_cls)
        cb = cb_cls(**cb_kwargs)
        assert isinstance(cb_cls, type)
        assert isinstance(cb, cb_cls)

    @pytest.mark.filterwarnings('ignore::UserWarning')
    def test_multiple_fit_start_and_end(self, cb_cls: type[Callback], dummy_state: State):
        """Test that callbacks do not crash when Event.FIT_START and Event.FIT_END is called multiple times."""
        maybe_patch_context = get_cb_patches(cb_cls)
        with maybe_patch_context:
            cb_kwargs = get_cb_kwargs(cb_cls)
            dummy_state.callbacks.append(cb_cls(**cb_kwargs))
            dummy_state.profiler = Profiler(
                schedule=lambda _: ProfilerAction.SKIP,
                trace_handlers=[],
                torch_prof_memory_filename=None,
            )
            dummy_state.profiler.bind_to_state(dummy_state)

            logger = Logger(dummy_state)
            engine = Engine(state=dummy_state, logger=logger)

            engine.run_event(Event.INIT)  # always runs just once per engine

            engine.run_event(Event.FIT_START)
            engine.run_event(Event.FIT_END)

            engine.run_event(Event.FIT_START)
            engine.run_event(Event.FIT_END)

    @pytest.mark.filterwarnings('ignore::UserWarning')
    def test_idempotent_close(self, cb_cls: type[Callback], dummy_state: State, clean_mlflow_runs):
        """Test that callbacks do not crash when .close() and .post_close() are called multiple times."""
        maybe_patch_context = get_cb_patches(cb_cls)
        with maybe_patch_context:
            cb_kwargs = get_cb_kwargs(cb_cls)
            dummy_state.callbacks.append(cb_cls(**cb_kwargs))
            dummy_state.profiler = Profiler(
                schedule=lambda _: ProfilerAction.SKIP,
                trace_handlers=[],
                torch_prof_memory_filename=None,
            )
            dummy_state.profiler.bind_to_state(dummy_state)

            logger = Logger(dummy_state)
            engine = Engine(state=dummy_state, logger=logger)

            maybe_patch_context = get_cb_patches(cb_cls)
            with maybe_patch_context:
                engine.run_event(Event.INIT)
                engine.close()
                engine.close()

    @pytest.mark.filterwarnings('ignore::UserWarning')
    def test_multiple_init_and_close(self, cb_cls: type[Callback], dummy_state: State, clean_mlflow_runs):
        """Test that callbacks do not crash when INIT/.close()/.post_close() are called multiple times in that order."""
        maybe_patch_context = get_cb_patches(cb_cls)
        with maybe_patch_context:
            cb_kwargs = get_cb_kwargs(cb_cls)
            dummy_state.callbacks.append(cb_cls(**cb_kwargs))
            dummy_state.profiler = Profiler(
                schedule=lambda _: ProfilerAction.SKIP,
                trace_handlers=[],
                torch_prof_memory_filename=None,
            )
            dummy_state.profiler.bind_to_state(dummy_state)

            logger = Logger(dummy_state)
            engine = Engine(state=dummy_state, logger=logger)

            maybe_patch_context = get_cb_patches(cb_cls)
            with maybe_patch_context:
                engine.run_event(Event.INIT)
                engine.close()
                # For good measure, also test idempotent close, in case if there are edge cases with a second call to INIT
                engine.close()

                # Create a new engine, since the engine does allow events to run after it has been closed
                engine = Engine(state=dummy_state, logger=logger)
                engine.close()
                # For good measure, also test idempotent close, in case if there are edge cases with a second call to INIT
                engine.close()


@pytest.mark.parametrize('cb_cls', get_cbs_and_marks(callbacks=True, loggers=True, profilers=True))
# Parameterized across @pytest.mark.remote as some loggers (e.g. wandb) support integration testing
@pytest.mark.parametrize(
    'device_train_microbatch_size,_remote',
    [(1, False), (2, False), pytest.param(1, True, marks=pytest.mark.remote)],
)
@pytest.mark.filterwarnings(r'ignore:The profiler is enabled:UserWarning')
class TestCallbackTrains:

    def _get_trainer(self, cb: Callback, device_train_microbatch_size: int):
        loggers = cb if isinstance(cb, LoggerDestination) else None
        callbacks = cb if not isinstance(cb, LoggerDestination) else None

        model, train_dataloader, eval_dataloader = get_cb_model_and_datasets(cb, dl_size=4, batch_size=2)

        return Trainer(
            model=model,
            train_dataloader=train_dataloader,
            eval_dataloader=eval_dataloader,
            max_duration=2,
            device_train_microbatch_size=device_train_microbatch_size,
            callbacks=callbacks,
            loggers=loggers,
            profiler=Profiler(
                schedule=lambda _: ProfilerAction.SKIP,
                trace_handlers=[],
                torch_prof_memory_filename=None,
            ),
        )

    @pytest.mark.filterwarnings('ignore::UserWarning')
    def test_trains(self, cb_cls: type[Callback], device_train_microbatch_size: int, _remote: bool, clean_mlflow_runs):
        del _remote  # unused. `_remote` must be passed through to parameterize the test markers.
        cb_kwargs = get_cb_kwargs(cb_cls)
        cb = cb_cls(**cb_kwargs)

        maybe_patch_context = get_cb_patches(cb_cls)
        with maybe_patch_context:
            trainer = self._get_trainer(cb, device_train_microbatch_size)
            trainer.fit()

    @pytest.mark.filterwarnings('ignore::UserWarning')
    def test_trains_multiple_calls(
        self,
        cb_cls: type[Callback],
        device_train_microbatch_size: int,
        _remote: bool,
        clean_mlflow_runs,
    ):
        """
        Tests that training with multiple fits complete. Note: future functional tests should test for idempotency (e.g functionally)
        """
        del _remote  # unused. `_remote` must be passed through to parameterize the test markers.
        cb_kwargs = get_cb_kwargs(cb_cls)
        cb = cb_cls(**cb_kwargs)

        maybe_patch_context = get_cb_patches(cb_cls)
        with maybe_patch_context:
            trainer = self._get_trainer(cb, device_train_microbatch_size)
            trainer.fit()

            assert trainer.state.max_duration is not None
            trainer.state.max_duration = cast(Time[int], trainer.state.max_duration * 2)

            trainer.fit()
