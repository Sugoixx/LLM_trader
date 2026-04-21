import time
import functools
import asyncio
from typing import Callable, Any
from src.config.loader import config
from src.utils.protocols import HasLogger

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


def profile_performance(func: Callable) -> Callable:
    """
    Decorator to measure and log the execution time of a method.
    Only active when logger_debug is True in config.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs) -> Any:
        # Check config at runtime to allow hot reloading
        if not config.LOGGER_DEBUG:
            return (
                await func(*args, **kwargs)
                if asyncio.iscoroutinefunction(func)
                else func(*args, **kwargs)
            )

        instance = args[0] if args else None
        logger = instance.logger if isinstance(instance, HasLogger) else None

        start_time = time.perf_counter()
        initial_memory = (
            psutil.Process().memory_info().rss / 1024 / 1024 if HAS_PSUTIL else 0
        )  # MB
        class_name = args[0].__class__.__name__ if args else ""
        method_name = func.__name__

        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            return result
        finally:
            end_time = time.perf_counter()
            duration = (end_time - start_time) * 1000  # Convert to ms
            final_memory = (
                psutil.Process().memory_info().rss / 1024 / 1024 if HAS_PSUTIL else 0
            )  # MB
            memory_delta = final_memory - initial_memory
            pending_tasks = len(
                [
                    t
                    for t in asyncio.all_tasks(asyncio.get_running_loop())
                    if not t.done()
                ]
            )

            # Identify if it's a "slow" operation (>1s) for highlight
            slow_marker = " [SLOW]" if duration > 1000 else ""
            memory_info = f", memory Δ: {memory_delta:+.2f}MB" if HAS_PSUTIL else ""
            async_info = f", pending tasks: {pending_tasks}"
            msg = f"Performance: {class_name}.{method_name} took {duration:.2f}ms{slow_marker}{memory_info}{async_info}"

            if logger:
                logger.debug(msg)
            else:
                # Fallback print if logger not found on instance
                print(f"[DEBUG] {msg}")

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs) -> Any:
        if not config.LOGGER_DEBUG:
            return func(*args, **kwargs)

        instance = args[0] if args else None
        logger = instance.logger if isinstance(instance, HasLogger) else None

        start_time = time.perf_counter()
        initial_memory = (
            psutil.Process().memory_info().rss / 1024 / 1024 if HAS_PSUTIL else 0
        )  # MB
        class_name = args[0].__class__.__name__ if args else ""
        method_name = func.__name__

        try:
            return func(*args, **kwargs)
        finally:
            end_time = time.perf_counter()
            duration = (end_time - start_time) * 1000
            final_memory = (
                psutil.Process().memory_info().rss / 1024 / 1024 if HAS_PSUTIL else 0
            )  # MB
            memory_delta = final_memory - initial_memory

            slow_marker = " [SLOW]" if duration > 1000 else ""
            memory_info = f", memory Δ: {memory_delta:+.2f}MB" if HAS_PSUTIL else ""
            msg = f"Performance: {class_name}.{method_name} took {duration:.2f}ms{slow_marker}{memory_info}"

            if logger:
                logger.debug(msg)
            else:
                print(f"[DEBUG] {msg}")

    # Return appropriate wrapper based on whether the original function is async
    if asyncio.iscoroutinefunction(func):
        return wrapper
    else:
        return sync_wrapper
