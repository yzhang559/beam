# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A workflow using BigQuery sources and sinks.

The workflow will read from a table that has the 'month' and 'tornado' fields as
part of the table schema (other additional fields are ignored). The 'month'
field is a number represented as a string (e.g., '23') and the 'tornado' field
is a boolean field.

The workflow will compute the number of tornadoes in each month and output
the results to a table (created if needed) with the following schema:
  - month: number
  - tornado_count: number

This example uses the default behavior for BigQuery source and sinks that
represents table rows as plain Python dictionaries.
"""

from __future__ import absolute_import

import logging

import google.cloud.dataflow as df
from google.cloud.dataflow.utils.options import add_option
from google.cloud.dataflow.utils.options import get_options


def count_tornadoes(input_data):
  """Workflow computing the number of tornadoes for each month that had one.

  Args:
    input_data: a PCollection of dictionaries representing table rows. Each
      dictionary will have a 'month' and a 'tornado' key as described in the
      module comment.

  Returns:
    A PCollection of dictionaries containing 'month' and 'tornado_count' keys.
    Months without tornadoes are skipped.
  """

  return (input_data
          | df.FlatMap(
              'months with tornadoes',
              lambda row: [(int(row['month']), 1)] if row['tornado'] else [])
          | df.CombinePerKey('monthly count', sum)
          | df.Map('format', lambda (k, v): {'month': k, 'tornado_count': v}))


def run(options=None):
  p = df.Pipeline(options=get_options(options))

  # Read the table rows into a PCollection.
  rows = p | df.io.Read('read', df.io.BigQuerySource(p.options.input))
  counts = count_tornadoes(rows)

  # Write the output using a "Write" transform that has side effects.
  # pylint: disable=expression-not-assigned
  counts | df.io.Write('write', df.io.BigQuerySink(
      p.options.output,
      schema='month:INTEGER, tornado_count:INTEGER',
      create_disposition=df.io.BigQueryDisposition.CREATE_IF_NEEDED,
      write_disposition=df.io.BigQueryDisposition.WRITE_TRUNCATE))

  # Run the pipeline (all operations are deferred until run() is called).
  p.run()


add_option(
    '--input', dest='input',
    default='clouddataflow-readonly:samples.weather_stations',
    help=('Input BigQuery table to process specified as: PROJECT:DATASET.TABLE '
          'or DATASET.TABLE.'))
add_option(
    '--output', dest='output', required=True,
    help=('Output BigQuery table for results specified as: '
          'PROJECT:DATASET.TABLE or DATASET.TABLE.'))


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  run()
