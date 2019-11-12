import argparse
from multiprocessing import Queue
import redis
import time
import os

import parsl
from parsl import python_app, bash_app
from parsl.executors import ThreadPoolExecutor
from parsl.executors import HighThroughputExecutor
from parsl.providers import LocalProvider
from parsl.config import Config
from parsl.data_provider.files import File
from concurrent.futures import Future

from redis_q import RedisQueue

import mpi_method_server

config_mac = Config(
    executors=[
        ThreadPoolExecutor(label="theta_mpi_launcher"),
        ThreadPoolExecutor(label="local_threads")
    ],
    strategy=None,
)

config = Config(
    executors=[
        HighThroughputExecutor(
            label="theta_mpi_launcher",
            # Max workers limits the concurrency exposed via mom node
            max_workers=2,
            provider=LocalProvider(
                init_blocks=1,
                max_blocks=1,
            ),
        ),
        ThreadPoolExecutor(label="local_threads")
    ],
    strategy=None,
)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--redishost", default="127.0.0.1",
                        help="Address at which the redis server can be reached")
    parser.add_argument("--redisport", default="6379",
                        help="Port on which redis is available")
    parser.add_argument("-d", "--debug", action='store_true',
                        help="Count of apps to launch")
    parser.add_argument("-m", "--mac", action='store_true',
                        help="Configure for Mac")
    args = parser.parse_args()

    if args.debug:
        parsl.set_stream_logger()

    if args.mac:
        parsl.load(config_mac)
    else:
        parsl.load(config)

    print('''This program creates an "MPI Method Server" that listens on an input queue and write on an output queue:

        input_queue --> mpi_method_server --> output_queue

To send it a request, add an entry to the input queue:
     run "python3 new_pump -p N" where N is an integer request
To access a result, remove it from the outout queue:
     run "python3 new_pull" (blocking) or "python3 new_pull -t T" (T an integer) to time out after T seconds
''')

    # input_queue --> mpi_method_server --> output_queue

    input_queue = RedisQueue(args.redishost, port=int(args.redisport), prefix='input')
    input_queue.connect()
    output_queue = RedisQueue(args.redishost, port=int(args.redisport), prefix='output')
    output_queue.connect()
    #value_server = RedisQueue(args.redishost, port=int(args.redisport), prefix='value')
    #value_server.connect()

    mms = mpi_method_server.MpiMethodServer(input_queue, output_queue)
    mms.main_loop()

    # Next up, we likely want to add the ability to create a value server and connect it to a method server, e.g.:
    #vs = value_server.ValueServer(output_queue)

    print("All done")