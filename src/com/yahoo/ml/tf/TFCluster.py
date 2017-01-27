# Copyright 2017 Yahoo Inc.
# Licensed under the terms of the Apache 2.0 license.
# Please see LICENSE file in the project root for terms.
"""
This module provides a high-level API to manage the TensorFlowOnSpark cluster.
"""

import logging
import operator
import threading
import time
import TFManager
import TFSparkNode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s (%(threadName)s-%(process)d) %(message)s",)

class InputMode(object):
  TENSORFLOW=0
  SPARK=1

class TFCluster(object):

  sc = None
  nodeRDD = None
  cluster_info = None
  input_mode = None
  queues = None

  def start(self, map_fun, tf_args):
      """
      Starts the TensorFlow main function on each node/executor (per cluster_spec) in a background thread on the driver.
      """
      logging.info("Starting TensorFlow")
      def _start():
          self.nodeRDD.foreachPartition(TFSparkNode.start(map_fun, tf_args, self.cluster_info, background=(self.input_mode == InputMode.SPARK)))

      # start TF on a background thread (on Spark driver)
      t = threading.Thread(target=_start)
      t.start()

      # sleep a bit to avoid having the next Spark job scheduled before the TFSparkNode.start() tasks on the background thread.
      time.sleep(5)

  def train(self, dataRDD, num_epochs=0, qname='input'):
      """
      Feeds Spark data partitions into the TensorFlow worker nodes.
      """
      logging.info("Feeding training data")
      assert(self.input_mode == InputMode.SPARK)
      assert(qname in self.queues)
      assert(num_epochs >= 0)

      # if num_epochs unspecified, pick an arbitrarily "large" number for now
      # TODO: calculate via dataRDD.count() / batch_size / max_steps
      if num_epochs == 0:
        num_epochs = 10

      rdds = []
      for i in range(num_epochs):
        rdds.append(dataRDD)

      unionRDD = self.sc.union(rdds)
      unionRDD.foreachPartition(TFSparkNode.train(self.cluster_info, qname))

  def test(self, dataRDD, qname='input'):
      """
      Feeds Spark data partitions into the TensorFlow worker nodes and returns an RDD of results.
      """
      logging.info("Feeding test data")
      assert(self.input_mode == InputMode.SPARK)
      assert(qname in self.queues)
      return dataRDD.mapPartitions(TFSparkNode.test(self.cluster_info, qname))

  def shutdown(self):
      """
      Stops TensorFlow nodes
      """
      logging.info("Stopping TensorFlow nodes")

      # identify ps/workers
      ps_list, worker_list = [], []
      for node in self.cluster_info:
          if node['job_name'] == 'ps':
              ps_list.append(node)
          else:
              worker_list.append(node)

      if self.input_mode == InputMode.SPARK:
        # in SPARK mode, the "shutdown" job will queue up behind the "data feeding" job and insert markers into data queues
        # to terminate their readers.
        workers = len(worker_list)
        workerRDD = self.sc.parallelize(range(workers), workers)
        workerRDD.foreachPartition(TFSparkNode.shutdown(self.cluster_info, self.queues))
      else:
        # in TENSORFLOW mode, there is no "data feeding" job, only a "start" job, so we must wait for the TensorFlow workers
        # to complete all tasks, while accounting for any PS nodes which currently run indefinitely.
        count = 0
        done = False
        if len(ps_list) > 0:
          while not done:
            st = self.sc.statusTracker()
            jobs = st.getActiveJobsIds()
            if len(jobs) > 0:
              stages = st.getActiveStageIds()
              for i in stages:
                si = st.getStageInfo(i)
                if si.numActiveTasks == len(ps_list):
                  # if we only have PS tasks left, check that we see this condition a couple times
                  count += 1
                  done = (count >= 3)
                  time.sleep(5)
            else:
              # should never happen, unless PS nodes somehow terminated
              logging.warn("PS node exited?")
              done = True

      # stop all PS nodes via control queues (skipped if no PS nodes)
      for node in ps_list:
          addr = node['addr']
          authkey = node['authkey']
          print("connecting to {0}, {1}".format(addr,authkey))
          m = TFManager.connect(addr, authkey)
          q = m.get_queue('control')
          q.put(None)

      # wait for PS nodes to exit
      time.sleep(10)

def reserve(sc, num_executors, num_ps, tensorboard=False, input_mode=InputMode.TENSORFLOW, queues=['input','output']):
    """
    Reserves ports, starts a multiprocessing.Manager per executor, and starts TensorBoard on worker/0 if requested.
    """
    logging.info("Reserving TFSparkNodes {0}".format("w/ TensorBoard" if tensorboard else ""))
    assert num_ps < num_executors

    # build a cluster_spec template using worker_nums
    spec = {}
    for i in range(num_executors):
        if i < num_ps:
            nodes = [] if 'ps' not in spec else spec['ps']
            nodes.append(i)
            spec['ps'] = nodes
        else:
            nodes = [] if 'worker' not in spec else spec['worker']
            nodes.append(i)
            spec['worker'] = nodes

    cluster = TFCluster()
    cluster.sc = sc
    cluster.nodeRDD = sc.parallelize(range(num_executors), num_executors)
    cluster.cluster_info = cluster.nodeRDD.mapPartitions(TFSparkNode.reserve(spec, tensorboard, queues)).collect()
    cluster.input_mode = input_mode
    cluster.queues = queues

    tb_url = None
    for node in cluster.cluster_info:
      print(node)
      if node['tb_port'] != 0:
        tb_url = "http://{0}:{1}".format(node['host'], node['tb_port'])

    if tb_url is not None:
      logging.info("TensorBoard running at: {0}".format(tb_url))

    return cluster

