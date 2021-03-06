"""Wrappers for Redis queues."""

import logging
from typing import Optional, Any, Tuple, Dict, Iterable

import redis

from colmena.exceptions import TimeoutException, KillSignalException
from colmena.models import Result

logger = logging.getLogger(__name__)


def _error_if_unconnected(f):
    def wrapper(queue: 'RedisQueue', *args, **kwargs) -> Any:
        if not queue.is_connected:
            raise ConnectionError('Not connected')
        return f(queue, *args, **kwargs)
    return wrapper


def make_queue_pairs(hostname: str, port: int = 6379, name='method', use_pickle: bool = False,
                     clean_slate: bool = True, topics: Optional[Iterable[str]] = None)\
        -> Tuple['ClientQueues', 'MethodServerQueues']:
    """Make a pair of queues for a server and client

    Args:
        hostname (str): Hostname of the Redis server
        port (int): Port on which to access Redis
        name (str): Name of the MethodServer
        clean_slate (bool): Whether to flush the queues before launching
        use_pickle (bool): Whether to use pickle to save Python objects before sending them
        topics ([str]): List of topics used when having the client filter different types of tasks
    Returns:
        (ClientQueues, MethodServerQueues): Pair of communicators set to use the correct channels
    """

    return (ClientQueues(hostname, port, name, use_pickle, topics),
            MethodServerQueues(hostname, port, name, topics=topics, clean_slate=clean_slate, use_pickle=use_pickle))


class RedisQueue(object):
    """A basic redis queue for communications used by the method server

    A queue is defined by its prefix and a "topic" designation.
    The full list of available topics is defined when creating the queue,
    and simplifies writing software that waits for only certain types
    of messages without needing to manage several "queue" objects.
    By default, the :meth:`get` methods for the queue
    listen on all topics and the :meth:`put` method pushes to the default topic.
    You can put messages into certain "topical" queues and wait for responses
    that are from a single topic.

    The queue only connects when the `connect` method is called to avoid
    issues with passing an object across processes."""

    def __init__(self, hostname: str, port: int = 6379, prefix='pipeline',
                 topics: Optional[Iterable[str]] = None):
        """
        Args:
            hostname (str): Hostname of the Redis server
            port (int): Port on which to access Redis
            prefix (str): Name of the Redis queue
            topics ([str]): List of special topics
        """
        self.hostname = hostname
        self.port = port
        self.redis_client = None
        self.prefix = prefix

        # Create the list of accepted topics
        if topics is None:
            topics = set()
        else:
            topics = set(topics)
        topics.add("default")
        assert not any('_' in t for t in topics), "Topic names may not contain underscores"
        self._all_queues = [f'{prefix}_{t}' for t in topics]

    def connect(self):
        """Connect to the Redis server"""
        try:
            if not self.redis_client:
                self.redis_client = redis.StrictRedis(
                    host=self.hostname, port=self.port, decode_responses=True)
                self.redis_client.ping()  # Ping is needed to detect if connection failed
        except redis.exceptions.ConnectionError:
            logger.warning(f"ConnectionError while trying to connect to Redis@{self.hostname}:{self.port}")
            raise

    @_error_if_unconnected
    def get(self, timeout: int = None, topic: Optional[str] = None) -> Optional[Tuple[str, str]]:
        """Get an item from the redis queue

        Args:
            timeout (int): Timeout for the blocking get in seconds
            topic (str): Which topical queue to read from. If ``None``, wait for all topics
        Returns:
            If timeout occurs, output is ``None``. Else:
            - (str) Topic of the item
            - (str) Value from the redis queue
        """

        # Get the topic(s)
        if topic is None:
            queues = self._all_queues
        else:
            queues = f'{self.prefix}_{topic}'
            assert queues in self._all_queues, f"Unrecognized topic: {topic}"

        # Wait for the result
        try:
            if timeout is None:
                queue, result = self.redis_client.blpop(queues)
            else:
                output = self.redis_client.blpop(queues, timeout=int(timeout))
                if output is None:
                    return output
                queue, result = output

            # Strip off the prefix for the queue
            return queue.split("_")[-1], result
        except redis.exceptions.ConnectionError:
            print(f"ConnectionError while trying to connect to Redis@{self.hostname}:{self.port}")
            raise

    @_error_if_unconnected
    def put(self, input_data: str, topic: str = 'default'):
        """Push data to a Redis queue

        Args:
            input_data (str): Message to be sent
            topic (str): Topic of the queue
        """

        # Make the queue name
        queue = f'{self.prefix}_{topic}'
        assert queue in self._all_queues, f'Unrecognized topic: {topic}'

        # Send it to the method server
        try:
            self.redis_client.rpush(queue, input_data)
        except redis.exceptions.ConnectionError:
            logger.warning(f"ConnectionError while trying to connect to Redis@{self.hostname}:{self.port}")
            raise

    @_error_if_unconnected
    def flush(self):
        """Flush the Redis queue"""
        for queue in self._all_queues:
            try:
                self.redis_client.delete(queue)
            except AttributeError:
                raise Exception("Queue is empty/flushed")
            except redis.exceptions.ConnectionError:
                logger.warning(f"ConnectionError while trying to connect to Redis@{self.hostname}:{self.port}")
                raise

    @property
    def is_connected(self):
        return self.redis_client is not None


class ClientQueues:
    """Wraps communication with the MethodServer"""

    def __init__(self, hostname: str, port: int = 6379, name: Optional[str] = None,
                 use_pickle: bool = False, topics: Optional[Iterable] = None):
        """
        Args:
            hostname (str): Hostname of the Redis server
            port (int): Port on which to access Redis
            name (int): Name of the MethodServer
            use_pickle (bool): Whether to use pickle to save Python objects before sending them
            topics ([str]): List of topics used when having the client filter different types of tasks
        """

        self.use_pickle = use_pickle

        # Make the queues
        self.outbound = RedisQueue(hostname, port, 'inputs' if name is None else f'{name}_inputs', topics=topics)
        self.inbound = RedisQueue(hostname, port, 'results' if name is None else f'{name}_results', topics=topics)

        # Attempt to connect
        self.outbound.connect()
        self.inbound.connect()

    def send_inputs(self, *input_args: Any, method: str = None,
                    input_kwargs: Optional[Dict[str, Any]] = None,
                    topic: str = 'default'):
        """Send inputs to be computed

        Args:
            *input_args (Any): Positional arguments to a function
            method (str): Name of the method to run. Optional
            input_kwargs (dict): Any keyword arguments for the function being run
            topic (str): Topic for the queue, which sets the topic for the result.
        """

        # Make fake kwargs, if needed
        if input_kwargs is None:
            input_kwargs = dict()

        # Create a new Result object
        result = Result((input_args, input_kwargs), method=method)

        # Push the serialized value to the method server
        if self.use_pickle:
            result.pickle_data()
        self.outbound.put(result.json(exclude_unset=True), topic=topic)
        logger.info(f'Client sent a {method} task with topic {topic}')

    def get_result(self, timeout: Optional[int] = None, topic: Optional[str] = None) -> Optional[Result]:
        """Get a value from the MethodServer

        Args:
            timeout (int): Timeout for waiting for a value
            topic (str): What topic of task to wait for. Set to ``None`` to pull all topics
        Returns:
            (Result) Result from a computation, or ``None`` if timeout is met
        """

        # Get a value
        output = self.inbound.get(timeout=timeout, topic=topic)
        logging.debug(f'Received value: {str(output)[:50]}')

        # If None, return because this is a timeout
        if output is None:
            return output
        topic, message = output

        # Parse the value and mark it as complete
        result_obj = Result.parse_raw(message)
        if self.use_pickle:
            result_obj.unpickle_data()
        result_obj.mark_result_received()

        # Some logging
        logger.info(f'Client received a {result_obj.method} result with topic {topic}')

        return result_obj

    def send_kill_signal(self):
        """Send the kill signal to the method server"""
        self.outbound.put("null")


class MethodServerQueues:
    """Communication wrapper for the MethodServer itself"""

    def __init__(self, hostname: str, port: int = 6379, name: Optional[str] = None,
                 clean_slate: bool = True, use_pickle: bool = False,
                 topics: Optional[Iterable[str]] = None):
        """
        Args:
            hostname (str): Hostname of the Redis server
            port (int): Port on which to access Redis
            name (str): Name of the MethodServer
            clean_slate (bool): Whether to flush the queues before launching
            use_pickle (bool): Whether to use pickle to save Python objects before sending them
            topics ([str]): List of topics used when having the client filter different types of tasks
        """

        self.use_pickle = use_pickle

        # Make the queues
        self.inbound = RedisQueue(hostname, port, 'inputs' if name is None else f'{name}_inputs', topics=topics)
        self.outbound = RedisQueue(hostname, port, 'results' if name is None else f'{name}_results', topics=topics)

        # Attempt to connect
        self.outbound.connect()
        self.inbound.connect()

        # Flush, if desired
        if clean_slate:
            self.outbound.flush()
            self.inbound.flush()

    def get_task(self, timeout: int = None) -> Tuple[str, Result]:
        """Get a task object

        Args:
            timeout (int): Timeout for waiting for a task
        Returns:
            - (str) Topic of the calculation. Used in defining which queue to use to send the results
            - (Result) Task description
        Raises:
            TimeoutException: If the timeout on the queue is reached
            KillSignalException: If the queue receives a kill signal
        """

        # Pull a record off of the queue
        output = self.inbound.get(timeout)

        # Return the kill signal
        if output is None:
            raise TimeoutException('Listening on task queue timed out')
        elif output[1] == "null":
            raise KillSignalException('Kill signal received on task queue')
        topic, message = output

        # Get the message
        task = Result.parse_raw(message)
        if self.use_pickle:
            task.unpickle_data()
        task.mark_input_received()
        return topic, task

    def send_result(self, result: Result, topic: str = 'default'):
        """Send a value to a client

        Args:
            result (Result): Result object to communicate back
            topic (str): Topic of the calculation
        """
        if self.use_pickle:
            result.pickle_data()
        self.outbound.put(result.json(), topic=topic)
