from collections import namedtuple
from datetime import date, timedelta
import logging
import re
from time import sleep

from pyhocon import ConfigTree  # noqa: F401
from typing import Dict, Optional  # noqa: F401

from databuilder.extractor.base_bigquery_extractor import BaseBigQueryExtractor

TableColumnUsageTuple = namedtuple('TableColumnUsageTuple', ['database', 'cluster', 'schema',
                                                             'table', 'column', 'email'])

LOGGER = logging.getLogger(__name__)


class BigQueryTableUsageExtractor(BaseBigQueryExtractor):
    """
    An aggregate extractor for bigquery table usage. This class takes the data from
    the stackdriver logging API by filtering on timestamp, bigquery_resource and looking
    for referencedTables in the response.
    """
    TIMESTAMP_KEY = 'timestamp'
    _DEFAULT_SCOPES = ('https://www.googleapis.com/auth/cloud-platform',)
    EMAIL_PATTERN = 'email_pattern'
    DELAY_TIME = 'delay_time'

    def init(self, conf):
        # type: (ConfigTree) -> None
        BaseBigQueryExtractor.init(self, conf)
        self.timestamp = conf.get_string(
            BigQueryTableUsageExtractor.TIMESTAMP_KEY,
            (date.today() - timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z'))

        self.email_pattern = conf.get_string(BigQueryTableUsageExtractor.EMAIL_PATTERN, None)
        self.delay_time = conf.get_int(BigQueryTableUsageExtractor.DELAY_TIME, 100)

        self.table_usage_counts = {}
        self._count_usage()
        self.iter = iter(self.table_usage_counts)

    def _count_usage(self):  # noqa: C901
        # type: () -> None
        count = 0
        for entry in self._retrieve_records():
            count += 1
            if count % self.pagesize == 0:
                LOGGER.info('Aggregated {} records'.format(count))

            try:
                job = entry['protoPayload']['serviceData']['jobCompletedEvent']['job']
            except Exception:
                # Skip the record if the record missing certain fields
                continue
            if job['jobStatus']['state'] != 'DONE':
                # This job seems not to have finished yet, so we ignore it.
                continue
            if len(job['jobStatus'].get('error', {})) > 0:
                # This job has errors, so we ignore it
                continue

            email = entry['protoPayload']['authenticationInfo']['principalEmail']
            refTables = job['jobStatistics'].get('referencedTables', None)

            if not refTables:
                # Query results can be cached and if the source tables remain untouched,
                # bigquery will return it from a 24 hour cache result instead. In that
                # case, referencedTables has been observed to be empty:
                # https://cloud.google.com/logging/docs/reference/audit/bigquery/rest/Shared.Types/AuditData#JobStatistics
                continue

            # if email filter is provided, only the email matched with filter will be recorded.
            if self.email_pattern:
                if not re.match(self.email_pattern, email):
                    # the usage account not match email pattern
                    continue

            numTablesProcessed = job['jobStatistics']['totalTablesProcessed']
            if len(refTables) != numTablesProcessed:
                LOGGER.warn('The number of tables listed in job {job_id} is not consistent'
                            .format(job_id=job['jobName']['jobId']))

            for refTable in refTables:
                key = TableColumnUsageTuple(database='bigquery',
                                            cluster=refTable['projectId'],
                                            schema=refTable['datasetId'],
                                            table=refTable['tableId'],
                                            column='*',
                                            email=email)

                new_count = self.table_usage_counts.get(key, 0) + 1
                self.table_usage_counts[key] = new_count

    def _retrieve_records(self):
        # type: () -> Optional[Dict]
        """
        Extracts bigquery log data by looking at the principalEmail in the
        authenticationInfo block and referencedTables in the jobStatistics.

        :return: Provides a record or None if no more to extract
        """
        body = {
            'resourceNames': [
                'projects/{project_id}'.format(project_id=self.project_id)
            ],
            'pageSize': self.pagesize,
            'filter': 'resource.type="bigquery_resource" AND '
                      'protoPayload.methodName="jobservice.jobcompleted" AND '
                      'timestamp >= "{timestamp}"'.format(timestamp=self.timestamp)
        }
        for page in self._page_over_results(body):
            for entry in page['entries']:
                yield(entry)

    def extract(self):
        # type: () -> Optional[tuple]
        try:
            key = next(self.iter)
            return key, self.table_usage_counts[key]
        except StopIteration:
            return None

    def _page_over_results(self, body):
        # type: (Dict) -> Optional[Dict]
        response = self.logging_service.entries().list(body=body).execute(
            num_retries=BigQueryTableUsageExtractor.NUM_RETRIES)
        while response:
            if 'entries' in response:
                yield response

            try:
                if 'nextPageToken' in response:
                    body['pageToken'] = response['nextPageToken']
                    response = self.logging_service.entries().list(body=body).execute(
                        num_retries=BigQueryTableUsageExtractor.NUM_RETRIES)
                else:
                    response = None
            except Exception:
                # Add a delay when BQ quota exceeds limitation
                sleep(self.delay_time)

    def get_scope(self):
        # type: () -> str
        return 'extractor.bigquery_table_usage'
