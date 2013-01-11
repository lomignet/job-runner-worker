import codecs
import json
import logging
import os
import signal
import tempfile
from datetime import datetime

import gevent_subprocess as subprocess
from pytz import utc

from job_runner_worker.config import config
from job_runner_worker.models import RunLog


logger = logging.getLogger(__name__)


def execute_run(run_queue, event_queue):
    """
    Execute runs from the ``run_queue``.

    :param run_queue:
        An instance of ``Queue`` to consume run instances from.

    :param event_queue:
        An instance of ``Queue`` to push events to.

    """
    logger.info('Starting run executer')

    for run in run_queue:

        file_desc, file_path = tempfile.mkstemp(
            dir=config.get('job_runner_worker', 'script_temp_path')
        )
        # seems there isn't support to open file descriptors directly in
        # utf-8 encoding
        os.fdopen(file_desc).close()

        file_obj = codecs.open(file_path, 'w', 'utf-8')
        file_obj.write(run.job.script_content.replace('\r', ''))
        file_obj.close()

        # get shebang from content of the script
        shebang = run.job.script_content.split('\n', 1)[0]
        executable = shebang.replace('#!', '').split()
        executable.append(file_path)

        logger.info('Starting run {0}'.format(run.resource_uri))
        did_run = False
        try:
            run.patch({'start_dts': datetime.now(utc).isoformat(' ')})

            sub_proc = subprocess.Popen(
                executable, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            event_queue.put(json.dumps(
                {'event': 'started', 'run_id': run.id, 'kind': 'run'}))

            run.patch({'pid': sub_proc.pid})
            did_run = True
            out, err = sub_proc.communicate()
        except Exception as e:
            out = 'Could not execute job: ' + str(e)
            event_queue.put(json.dumps(
                {'event': 'started', 'run_id': run.id, 'kind': 'run'}))

        log_output = _truncate_log(out)

        logger.info('Run {0} ended'.format(run.resource_uri))
        run_log = RunLog(
            config.get('job_runner_worker', 'run_log_resource_uri'))
        run_log.post({
            'run': '{0}{1}/'.format(
                config.get('job_runner_worker', 'run_resource_uri'),
                run.id
            ),
            'content': log_output
        })
        run.patch({
            'return_dts': datetime.now(utc).isoformat(' '),
            'return_success':
            False if did_run is False or sub_proc.returncode else True,
        })
        event_queue.put(json.dumps(
            {'event': 'returned', 'run_id': run.id, 'kind': 'run'}))
        os.remove(file_path)


def kill_run(kill_queue, event_queue):
    """
    Execute kill-requests from the ``kill_queue``.

    :param kill_queue:
        An instance of ``Queue`` to consume kill-requests from.

    :param event_queue:
        An instance of ``Queue`` to push events to.

    """
    logger.info('Starting executor for kill-requests')

    for kill_request in kill_queue:
        run = kill_request.run

        _kill_pid_tree(run.pid)
        kill_request.patch({'execute_dts': datetime.now(utc).isoformat(' ')})
        event_queue.put(json.dumps({
            'event': 'executed',
            'kill_request_id': kill_request.id,
            'kind': 'kill_request'
        }))


def _kill_pid_tree(pid):
    """
    Kill a given ``pid`` including its tree of children.

    :param pid:
        An ``int`` representing the parent ``PID``.

    """
    children = _get_child_pids(pid)
    for child_pid in children:
        _kill_pid_tree(child_pid)

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        logger.exception(
            'Error while killing {0}, process already finished?'.format(
                pid)
        )


def _get_child_pids(pid):
    """
    Return the list of children ``PID``s for the given parent ``pid``.

    :param pid:
        An ``int`` representing the parent ``PID``.

    :return:
        A ``list`` of children ``PIDS``s (if any).

    """
    sub_proc = subprocess.Popen(
        ['ps', '-o', 'pid', '--ppid', str(pid), '--noheaders'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ret_code = sub_proc.wait()

    if ret_code == 0:
        out, err = sub_proc.communicate()
        return [int(x) for x in out.split('\n')[:-1]]

    return []


def _truncate_log(log_txt):
    """
    Truncate the ``log_txt`` in case it exeeds the max. log size.

    :param log_txt:
        A ``str``.

    """
    max_log_bytes = config.getint('job_runner_worker', 'max_log_bytes')

    if len(log_txt) > max_log_bytes:
        top_length = int(max_log_bytes * 0.2)
        bottom_length = int(max_log_bytes * 0.8)

        log_txt = '{0}\n\n[truncated]\n\n{1}'.format(
            log_txt[:top_length],
            log_txt[len(log_txt) - bottom_length:]
        )

    return log_txt
