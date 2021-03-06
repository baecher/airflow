# -*- coding: utf-8 -*-
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import copy
import logging
import os
import unittest
import unittest.mock
from collections import namedtuple
from datetime import date, timedelta

from airflow.exceptions import AirflowException
from airflow.models import DAG, DagRun, TaskInstance as TI
from airflow.operators.dummy_operator import DummyOperator
from airflow.operators.python import BranchPythonOperator, PythonOperator, ShortCircuitOperator
from airflow.utils import timezone
from airflow.utils.session import create_session
from airflow.utils.state import State

DEFAULT_DATE = timezone.datetime(2016, 1, 1)
END_DATE = timezone.datetime(2016, 1, 2)
INTERVAL = timedelta(hours=12)
FROZEN_NOW = timezone.datetime(2016, 1, 2, 12, 1, 1)

TI_CONTEXT_ENV_VARS = ['AIRFLOW_CTX_DAG_ID',
                       'AIRFLOW_CTX_TASK_ID',
                       'AIRFLOW_CTX_EXECUTION_DATE',
                       'AIRFLOW_CTX_DAG_RUN_ID']


class Call:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def build_recording_function(calls_collection):
    """
    We can not use a Mock instance as a PythonOperator callable function or some tests fail with a
    TypeError: Object of type Mock is not JSON serializable
    Then using this custom function recording custom Call objects for further testing
    (replacing Mock.assert_called_with assertion method)
    """
    def recording_function(*args, **kwargs):
        calls_collection.append(Call(*args, **kwargs))
    return recording_function


class TestPythonBase(unittest.TestCase):
    """Base test class for TestPythonOperator and TestPythonSensor classes"""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        with create_session() as session:
            session.query(DagRun).delete()
            session.query(TI).delete()

    def setUp(self):
        super().setUp()
        self.dag = DAG(
            'test_dag',
            default_args={
                'owner': 'airflow',
                'start_date': DEFAULT_DATE})
        self.addCleanup(self.dag.clear)
        self.clear_run()
        self.addCleanup(self.clear_run)

    def tearDown(self):
        super().tearDown()

        with create_session() as session:
            session.query(DagRun).delete()
            session.query(TI).delete()

    def clear_run(self):
        self.run = False

    def _assert_calls_equal(self, first, second):
        self.assertIsInstance(first, Call)
        self.assertIsInstance(second, Call)
        self.assertTupleEqual(first.args, second.args)
        # eliminate context (conf, dag_run, task_instance, etc.)
        test_args = ["an_int", "a_date", "a_templated_string"]
        first.kwargs = {
            key: value
            for (key, value) in first.kwargs.items()
            if key in test_args
        }
        second.kwargs = {
            key: value
            for (key, value) in second.kwargs.items()
            if key in test_args
        }
        self.assertDictEqual(first.kwargs, second.kwargs)


@unittest.mock.patch('os.environ', {
    'AIRFLOW_CTX_DAG_ID': None,
    'AIRFLOW_CTX_TASK_ID': None,
    'AIRFLOW_CTX_EXECUTION_DATE': None,
    'AIRFLOW_CTX_DAG_RUN_ID': None
})
class TestPythonOperator(TestPythonBase):

    def do_run(self):
        self.run = True

    def is_run(self):
        return self.run

    def test_python_operator_run(self):
        """Tests that the python callable is invoked on task run."""
        task = PythonOperator(
            python_callable=self.do_run,
            task_id='python_operator',
            dag=self.dag)
        self.assertFalse(self.is_run())
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)
        self.assertTrue(self.is_run())

    def test_python_operator_python_callable_is_callable(self):
        """Tests that PythonOperator will only instantiate if
        the python_callable argument is callable."""
        not_callable = {}
        with self.assertRaises(AirflowException):
            PythonOperator(
                python_callable=not_callable,
                task_id='python_operator',
                dag=self.dag)
        not_callable = None
        with self.assertRaises(AirflowException):
            PythonOperator(
                python_callable=not_callable,
                task_id='python_operator',
                dag=self.dag)

    def test_python_callable_arguments_are_templatized(self):
        """Test PythonOperator op_args are templatized"""
        recorded_calls = []

        # Create a named tuple and ensure it is still preserved
        # after the rendering is done
        Named = namedtuple('Named', ['var1', 'var2'])
        named_tuple = Named('{{ ds }}', 'unchanged')

        task = PythonOperator(
            task_id='python_operator',
            # a Mock instance cannot be used as a callable function or test fails with a
            # TypeError: Object of type Mock is not JSON serializable
            python_callable=build_recording_function(recorded_calls),
            op_args=[
                4,
                date(2019, 1, 1),
                "dag {{dag.dag_id}} ran on {{ds}}.",
                named_tuple
            ],
            dag=self.dag)

        self.dag.create_dagrun(
            run_id='manual__' + DEFAULT_DATE.isoformat(),
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
            state=State.RUNNING
        )
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        ds_templated = DEFAULT_DATE.date().isoformat()
        self.assertEqual(1, len(recorded_calls))
        self._assert_calls_equal(
            recorded_calls[0],
            Call(4,
                 date(2019, 1, 1),
                 "dag {} ran on {}.".format(self.dag.dag_id, ds_templated),
                 Named(ds_templated, 'unchanged'))
        )

    def test_python_callable_keyword_arguments_are_templatized(self):
        """Test PythonOperator op_kwargs are templatized"""
        recorded_calls = []

        task = PythonOperator(
            task_id='python_operator',
            # a Mock instance cannot be used as a callable function or test fails with a
            # TypeError: Object of type Mock is not JSON serializable
            python_callable=build_recording_function(recorded_calls),
            op_kwargs={
                'an_int': 4,
                'a_date': date(2019, 1, 1),
                'a_templated_string': "dag {{dag.dag_id}} ran on {{ds}}."
            },
            dag=self.dag)

        self.dag.create_dagrun(
            run_id='manual__' + DEFAULT_DATE.isoformat(),
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
            state=State.RUNNING
        )
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        self.assertEqual(1, len(recorded_calls))
        self._assert_calls_equal(
            recorded_calls[0],
            Call(an_int=4,
                 a_date=date(2019, 1, 1),
                 a_templated_string="dag {} ran on {}.".format(
                     self.dag.dag_id, DEFAULT_DATE.date().isoformat()))
        )

    def test_python_operator_shallow_copy_attr(self):
        not_callable = lambda x: x
        original_task = PythonOperator(
            python_callable=not_callable,
            task_id='python_operator',
            op_kwargs={'certain_attrs': ''},
            dag=self.dag
        )
        new_task = copy.deepcopy(original_task)
        # shallow copy op_kwargs
        self.assertEqual(id(original_task.op_kwargs['certain_attrs']),
                         id(new_task.op_kwargs['certain_attrs']))
        # shallow copy python_callable
        self.assertEqual(id(original_task.python_callable),
                         id(new_task.python_callable))

    def _env_var_check_callback(self):
        self.assertEqual('test_dag', os.environ['AIRFLOW_CTX_DAG_ID'])
        self.assertEqual('hive_in_python_op', os.environ['AIRFLOW_CTX_TASK_ID'])
        self.assertEqual(DEFAULT_DATE.isoformat(),
                         os.environ['AIRFLOW_CTX_EXECUTION_DATE'])
        self.assertEqual('manual__' + DEFAULT_DATE.isoformat(),
                         os.environ['AIRFLOW_CTX_DAG_RUN_ID'])

    def test_echo_env_variables(self):
        """
        Test that env variables are exported correctly to the
        python callback in the task.
        """
        self.dag.create_dagrun(
            run_id='manual__' + DEFAULT_DATE.isoformat(),
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
            state=State.RUNNING,
            external_trigger=False,
        )

        op = PythonOperator(task_id='hive_in_python_op',
                            dag=self.dag,
                            python_callable=self._env_var_check_callback)
        op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

    def test_conflicting_kwargs(self):
        self.dag.create_dagrun(
            run_id='manual__' + DEFAULT_DATE.isoformat(),
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
            state=State.RUNNING,
            external_trigger=False,
        )

        # dag is not allowed since it is a reserved keyword
        def func(dag):
            # An ValueError should be triggered since we're using dag as a
            # reserved keyword
            raise RuntimeError("Should not be triggered, dag: {}".format(dag))

        python_operator = PythonOperator(
            task_id='python_operator',
            op_args=[1],
            python_callable=func,
            dag=self.dag
        )

        with self.assertRaises(ValueError) as context:
            python_operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)
            self.assertTrue('dag' in context.exception, "'dag' not found in the exception")

    def test_context_with_conflicting_op_args(self):
        self.dag.create_dagrun(
            run_id='manual__' + DEFAULT_DATE.isoformat(),
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
            state=State.RUNNING,
            external_trigger=False,
        )

        def func(custom, dag):
            self.assertEqual(1, custom, "custom should be 1")
            self.assertIsNotNone(dag, "dag should be set")

        python_operator = PythonOperator(
            task_id='python_operator',
            op_kwargs={'custom': 1},
            python_callable=func,
            dag=self.dag
        )
        python_operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

    def test_context_with_kwargs(self):
        self.dag.create_dagrun(
            run_id='manual__' + DEFAULT_DATE.isoformat(),
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
            state=State.RUNNING,
            external_trigger=False,
        )

        def func(**context):
            # check if context is being set
            self.assertGreater(len(context), 0, "Context has not been injected")

        python_operator = PythonOperator(
            task_id='python_operator',
            op_kwargs={'custom': 1},
            python_callable=func,
            dag=self.dag
        )
        python_operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)


class TestBranchOperator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        with create_session() as session:
            session.query(DagRun).delete()
            session.query(TI).delete()

    def setUp(self):
        self.dag = DAG('branch_operator_test',
                       default_args={
                           'owner': 'airflow',
                           'start_date': DEFAULT_DATE},
                       schedule_interval=INTERVAL)

        self.branch_1 = DummyOperator(task_id='branch_1', dag=self.dag)
        self.branch_2 = DummyOperator(task_id='branch_2', dag=self.dag)
        self.branch_3 = None

    def tearDown(self):
        super().tearDown()

        with create_session() as session:
            session.query(DagRun).delete()
            session.query(TI).delete()

    def test_without_dag_run(self):
        """This checks the defensive against non existent tasks in a dag run"""
        branch_op = BranchPythonOperator(task_id='make_choice',
                                         dag=self.dag,
                                         python_callable=lambda: 'branch_1')
        self.branch_1.set_upstream(branch_op)
        self.branch_2.set_upstream(branch_op)
        self.dag.clear()

        branch_op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        with create_session() as session:
            tis = session.query(TI).filter(
                TI.dag_id == self.dag.dag_id,
                TI.execution_date == DEFAULT_DATE
            )

            for ti in tis:
                if ti.task_id == 'make_choice':
                    self.assertEqual(ti.state, State.SUCCESS)
                elif ti.task_id == 'branch_1':
                    # should exist with state None
                    self.assertEqual(ti.state, State.NONE)
                elif ti.task_id == 'branch_2':
                    self.assertEqual(ti.state, State.SKIPPED)
                else:
                    raise Exception

    def test_branch_list_without_dag_run(self):
        """This checks if the BranchPythonOperator supports branching off to a list of tasks."""
        branch_op = BranchPythonOperator(task_id='make_choice',
                                         dag=self.dag,
                                         python_callable=lambda: ['branch_1', 'branch_2'])
        self.branch_1.set_upstream(branch_op)
        self.branch_2.set_upstream(branch_op)
        self.branch_3 = DummyOperator(task_id='branch_3', dag=self.dag)
        self.branch_3.set_upstream(branch_op)
        self.dag.clear()

        branch_op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        with create_session() as session:
            tis = session.query(TI).filter(
                TI.dag_id == self.dag.dag_id,
                TI.execution_date == DEFAULT_DATE
            )

            expected = {
                "make_choice": State.SUCCESS,
                "branch_1": State.NONE,
                "branch_2": State.NONE,
                "branch_3": State.SKIPPED,
            }

            for ti in tis:
                if ti.task_id in expected:
                    self.assertEqual(ti.state, expected[ti.task_id])
                else:
                    raise Exception

    def test_with_dag_run(self):
        branch_op = BranchPythonOperator(task_id='make_choice',
                                         dag=self.dag,
                                         python_callable=lambda: 'branch_1')

        self.branch_1.set_upstream(branch_op)
        self.branch_2.set_upstream(branch_op)
        self.dag.clear()

        dr = self.dag.create_dagrun(
            run_id="manual__",
            start_date=timezone.utcnow(),
            execution_date=DEFAULT_DATE,
            state=State.RUNNING
        )

        branch_op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        tis = dr.get_task_instances()
        for ti in tis:
            if ti.task_id == 'make_choice':
                self.assertEqual(ti.state, State.SUCCESS)
            elif ti.task_id == 'branch_1':
                self.assertEqual(ti.state, State.NONE)
            elif ti.task_id == 'branch_2':
                self.assertEqual(ti.state, State.SKIPPED)
            else:
                raise Exception

    def test_with_skip_in_branch_downstream_dependencies(self):
        branch_op = BranchPythonOperator(task_id='make_choice',
                                         dag=self.dag,
                                         python_callable=lambda: 'branch_1')

        branch_op >> self.branch_1 >> self.branch_2
        branch_op >> self.branch_2
        self.dag.clear()

        dr = self.dag.create_dagrun(
            run_id="manual__",
            start_date=timezone.utcnow(),
            execution_date=DEFAULT_DATE,
            state=State.RUNNING
        )

        branch_op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        tis = dr.get_task_instances()
        for ti in tis:
            if ti.task_id == 'make_choice':
                self.assertEqual(ti.state, State.SUCCESS)
            elif ti.task_id == 'branch_1':
                self.assertEqual(ti.state, State.NONE)
            elif ti.task_id == 'branch_2':
                self.assertEqual(ti.state, State.NONE)
            else:
                raise Exception

    def test_with_skip_in_branch_downstream_dependencies2(self):
        branch_op = BranchPythonOperator(task_id='make_choice',
                                         dag=self.dag,
                                         python_callable=lambda: 'branch_2')

        branch_op >> self.branch_1 >> self.branch_2
        branch_op >> self.branch_2
        self.dag.clear()

        dr = self.dag.create_dagrun(
            run_id="manual__",
            start_date=timezone.utcnow(),
            execution_date=DEFAULT_DATE,
            state=State.RUNNING
        )

        branch_op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        tis = dr.get_task_instances()
        for ti in tis:
            if ti.task_id == 'make_choice':
                self.assertEqual(ti.state, State.SUCCESS)
            elif ti.task_id == 'branch_1':
                self.assertEqual(ti.state, State.SKIPPED)
            elif ti.task_id == 'branch_2':
                self.assertEqual(ti.state, State.NONE)
            else:
                raise Exception

    def test_xcom_push(self):
        branch_op = BranchPythonOperator(task_id='make_choice',
                                         dag=self.dag,
                                         python_callable=lambda: 'branch_1')

        self.branch_1.set_upstream(branch_op)
        self.branch_2.set_upstream(branch_op)
        self.dag.clear()

        dr = self.dag.create_dagrun(
            run_id="manual__",
            start_date=timezone.utcnow(),
            execution_date=DEFAULT_DATE,
            state=State.RUNNING
        )

        branch_op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        tis = dr.get_task_instances()
        for ti in tis:
            if ti.task_id == 'make_choice':
                self.assertEqual(
                    ti.xcom_pull(task_ids='make_choice'), 'branch_1')


class TestShortCircuitOperator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        with create_session() as session:
            session.query(DagRun).delete()
            session.query(TI).delete()

    def tearDown(self):
        super().tearDown()

        with create_session() as session:
            session.query(DagRun).delete()
            session.query(TI).delete()

    def test_without_dag_run(self):
        """This checks the defensive against non existent tasks in a dag run"""
        value = False
        dag = DAG('shortcircuit_operator_test_without_dag_run',
                  default_args={
                      'owner': 'airflow',
                      'start_date': DEFAULT_DATE
                  },
                  schedule_interval=INTERVAL)
        short_op = ShortCircuitOperator(task_id='make_choice',
                                        dag=dag,
                                        python_callable=lambda: value)
        branch_1 = DummyOperator(task_id='branch_1', dag=dag)
        branch_1.set_upstream(short_op)
        branch_2 = DummyOperator(task_id='branch_2', dag=dag)
        branch_2.set_upstream(branch_1)
        upstream = DummyOperator(task_id='upstream', dag=dag)
        upstream.set_downstream(short_op)
        dag.clear()

        short_op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        with create_session() as session:
            tis = session.query(TI).filter(
                TI.dag_id == dag.dag_id,
                TI.execution_date == DEFAULT_DATE
            )

            for ti in tis:
                if ti.task_id == 'make_choice':
                    self.assertEqual(ti.state, State.SUCCESS)
                elif ti.task_id == 'upstream':
                    # should not exist
                    raise Exception
                elif ti.task_id == 'branch_1' or ti.task_id == 'branch_2':
                    self.assertEqual(ti.state, State.SKIPPED)
                else:
                    raise Exception

            value = True
            dag.clear()

            short_op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)
            for ti in tis:
                if ti.task_id == 'make_choice':
                    self.assertEqual(ti.state, State.SUCCESS)
                elif ti.task_id == 'upstream':
                    # should not exist
                    raise Exception
                elif ti.task_id == 'branch_1' or ti.task_id == 'branch_2':
                    self.assertEqual(ti.state, State.NONE)
                else:
                    raise Exception

    def test_with_dag_run(self):
        value = False
        dag = DAG('shortcircuit_operator_test_with_dag_run',
                  default_args={
                      'owner': 'airflow',
                      'start_date': DEFAULT_DATE
                  },
                  schedule_interval=INTERVAL)
        short_op = ShortCircuitOperator(task_id='make_choice',
                                        dag=dag,
                                        python_callable=lambda: value)
        branch_1 = DummyOperator(task_id='branch_1', dag=dag)
        branch_1.set_upstream(short_op)
        branch_2 = DummyOperator(task_id='branch_2', dag=dag)
        branch_2.set_upstream(branch_1)
        upstream = DummyOperator(task_id='upstream', dag=dag)
        upstream.set_downstream(short_op)
        dag.clear()

        logging.error("Tasks %s", dag.tasks)
        dr = dag.create_dagrun(
            run_id="manual__",
            start_date=timezone.utcnow(),
            execution_date=DEFAULT_DATE,
            state=State.RUNNING
        )

        upstream.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)
        short_op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        tis = dr.get_task_instances()
        self.assertEqual(len(tis), 4)
        for ti in tis:
            if ti.task_id == 'make_choice':
                self.assertEqual(ti.state, State.SUCCESS)
            elif ti.task_id == 'upstream':
                self.assertEqual(ti.state, State.SUCCESS)
            elif ti.task_id == 'branch_1' or ti.task_id == 'branch_2':
                self.assertEqual(ti.state, State.SKIPPED)
            else:
                raise Exception

        value = True
        dag.clear()
        dr.verify_integrity()
        upstream.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)
        short_op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        tis = dr.get_task_instances()
        self.assertEqual(len(tis), 4)
        for ti in tis:
            if ti.task_id == 'make_choice':
                self.assertEqual(ti.state, State.SUCCESS)
            elif ti.task_id == 'upstream':
                self.assertEqual(ti.state, State.SUCCESS)
            elif ti.task_id == 'branch_1' or ti.task_id == 'branch_2':
                self.assertEqual(ti.state, State.NONE)
            else:
                raise Exception
