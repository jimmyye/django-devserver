"""
Based on initial work from django-debug-toolbar
"""
import re

from datetime import datetime

try:
    from django.db import connections
except ImportError:
    # Django version < 1.2
    from django.db import connection
    connections = {'default': connection}

try:
    from django.db.backends import utils
except ImportError:
    from django.db.backends import util as utils  # Django < 1.7
from django.conf import settings as django_settings
#from django.template import Node

from devserver.modules import DevServerModule
#from devserver.utils.stack import tidy_stacktrace, get_template_info
from devserver.utils.time import ms_from_timedelta
from devserver import settings

try:
    import sqlparse
except ImportError:
    class sqlparse:
        @staticmethod
        def format(text, *args, **kwargs):
            return text


_sql_fields_re = re.compile(r'SELECT .*? FROM')
_sql_aggregates_re = re.compile(r'SELECT .*?(COUNT|SUM|AVERAGE|MIN|MAX).*? FROM')
_sql_in_numbers_re = re.compile(r'IN\s*\(([\d\s,]+)\)')


def truncate_sql(sql, aggregates=True):
    def truncate_number_list(match):
        numbers = match.group(1).split(',')
        result = [x.strip() for x in numbers[:3]]
        if len(numbers) > len(result):
            result.append('...')
        return u'IN (%s)' % (u', '.join(result))
    sql = _sql_in_numbers_re.sub(truncate_number_list, sql)

    if not aggregates and _sql_aggregates_re.match(sql):
        return sql
    return _sql_fields_re.sub('SELECT ... FROM', sql)

# # TODO:This should be set in the toolbar loader as a default and panels should
# # get a copy of the toolbar object with access to its config dictionary
# SQL_WARNING_THRESHOLD = getattr(settings, 'DEVSERVER_CONFIG', {}) \
#                             .get('SQL_WARNING_THRESHOLD', 500)

try:
    from debug_toolbar.panels.sql import DatabaseStatTracker
    debug_toolbar = True
except ImportError:
    debug_toolbar = False
    import django
    version = float('.'.join([str(x) for x in django.VERSION[:2]]))
    if version >= 1.6:
        DatabaseStatTracker = utils.CursorWrapper
    else:
        DatabaseStatTracker = utils.CursorDebugWrapper


class DatabaseStatTracker(DatabaseStatTracker):
    """
    Replacement for CursorDebugWrapper which outputs information as it happens.
    """
    logger = None

    def execute(self, sql, params=()):
        formatted_sql = sql % (params if isinstance(params, dict) else tuple(params))
        if self.logger:
            message = formatted_sql
            if settings.DEVSERVER_FILTER_SQL:
                if any(filter_.search(message) for filter_ in settings.DEVSERVER_FILTER_SQL):
                    message = None
            if message is not None:
                if settings.DEVSERVER_TRUNCATE_SQL:
                    message = truncate_sql(message, aggregates=settings.DEVSERVER_TRUNCATE_AGGREGATES)
                message = sqlparse.format(message, reindent=True, keyword_case='upper')

        start = datetime.now()

        try:
            return super(DatabaseStatTracker, self).execute(sql, params)
        finally:
            stop = datetime.now()
            duration = ms_from_timedelta(stop - start)

            if self.logger and (not settings.DEVSERVER_SQL_MIN_DURATION
                    or duration > settings.DEVSERVER_SQL_MIN_DURATION):
                if message is not None:
                    self.logger.debug(message, rowcount=self.cursor.rowcount, duration=duration)

            if version >= 1.6 or not (debug_toolbar or django_settings.DEBUG):
                self.db.queries_log.append({
                    'sql': formatted_sql,
                    'time': duration,
                })

    def executemany(self, sql, param_list):
        start = datetime.now()
        try:
            return super(DatabaseStatTracker, self).executemany(sql, param_list)
        finally:
            stop = datetime.now()
            duration = ms_from_timedelta(stop - start)

            if self.logger:
                message = sqlparse.format(sql, reindent=True, keyword_case='upper')

                message = 'Executed %s times\n' % message

                self.logger.debug(message, duration=duration)
                self.logger.debug('Found %s matching rows', self.cursor.rowcount, duration=duration, id='query')

            if version >= 1.6 or not (debug_toolbar or settings.DEBUG):
                self.db.queries_log.append({
                    'sql': '%s times: %s' % (len(param_list), sql),
                    'time': duration,
                })


class SQLRealTimeModule(DevServerModule):
    """
    Outputs SQL queries as they happen.
    """

    logger_name = 'sql'

    def process_init(self, request):
        if not issubclass(utils.CursorDebugWrapper, DatabaseStatTracker):
            self.old_cursor = utils.CursorDebugWrapper
            utils.CursorDebugWrapper = DatabaseStatTracker
        DatabaseStatTracker.logger = self.logger

    def process_complete(self, request):
        if issubclass(utils.CursorDebugWrapper, DatabaseStatTracker):
            utils.CursorDebugWrapper = self.old_cursor


class SQLSummaryModule(DevServerModule):
    """
    Outputs a summary SQL queries.
    """

    logger_name = 'sql'

    def process_complete(self, request):
        queries = [
            q for alias in connections
            for q in connections[alias].queries
        ]
        num_queries = len(queries)
        if num_queries:
            unique = set([s['sql'] for s in queries])
            self.logger.info('%(calls)s queries with %(dupes)s duplicates' % dict(
                calls=num_queries,
                dupes=num_queries - len(unique),
            ), duration=sum(float(c.get('time', 0)) for c in queries))
