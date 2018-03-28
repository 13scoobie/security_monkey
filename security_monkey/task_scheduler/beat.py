"""
.. module: security_monkey.task_scheduler.beat
    :platform: Unix
    :synopsis: Sets up the Celery task scheduling for watching, auditing, and reporting changes in the environment.

.. version:: $$VERSION$$
.. moduleauthor:: Mike Grima <mgrima@netflix.com>
"""
import traceback

from security_monkey.reporter import Reporter

from security_monkey import app, sentry
from security_monkey.datastore import store_exception, Account
from security_monkey.task_scheduler.util import CELERY, setup
from security_monkey.task_scheduler.tasks import task_account_tech, clear_expired_exceptions


def purge_it():
    """Purge the existing Celery queue"""
    app.logger.debug("Purging the Celery tasks awaiting to execute")
    CELERY.control.purge()
    app.logger.debug("Completed the Celery purge.")


@CELERY.on_after_configure.connect
def setup_the_tasks(sender, **kwargs):
    setup()

    # Purge out all current tasks waiting to execute:
    purge_it()

    # Add all the tasks:
    try:
        accounts = Account.query.filter(Account.third_party == False).filter(Account.active == True).all()  # noqa
        for account in accounts:
            app.logger.info("[ ] Scheduling tasks for {type} account: {name}".format(type=account.type.name,
                                                                                     name=account.name))
            rep = Reporter(account=account.name)
            for monitor in rep.all_monitors:
                if monitor.watcher:
                    interval = monitor.watcher.get_interval() * 60
                    if not interval:
                        app.logger.debug("[/] Skipping watcher for technology: {} because it is set for external "
                                         "monitoring.".format(monitor.watcher.index))
                        continue

                    app.logger.debug("[{}] Scheduling for technology: {}".format(account.type.name,
                                                                                 monitor.watcher.index))

                    # Start the task immediately:
                    task_account_tech.apply_async((account.name, monitor.watcher.index))
                    app.logger.debug("[-->] Scheduled immediate task")

                    # Schedule it based on the schedule:
                    sender.add_periodic_task(interval, task_account_tech.s(account.name, monitor.watcher.index))
                    app.logger.debug("[+] Scheduled task to occur every {} minutes".format(interval))

                    # TODO: Due to a bug with Celery (https://github.com/celery/celery/issues/4041) we temporarily
                    #       disabled this to avoid many duplicate events from getting added.
                    # Also schedule a manual audit changer just in case it doesn't properly
                    # audit (only for non-batched):
                    # if not monitor.batch_support:
                    #     sender.add_periodic_task(
                    #         crontab(hour=10, day_of_week="mon-fri"), task_audit.s(account.name, monitor.watcher.index))
                    #     app.logger.debug("[+] Scheduled task for tech: {} for audit".format(monitor.watcher.index))
                    #
                    # app.logger.debug("[{}] Completed scheduling for technology: {}".format(account.name,
                    #                                                                        monitor.watcher.index))

            app.logger.debug("[+] Completed scheduling tasks for account: {}".format(account.name))

        # Schedule the task for clearing out old exceptions:
        app.logger.info("Scheduling task to clear out old exceptions.")

        # Run every 24 hours (and clear it now):
        clear_expired_exceptions.apply_async()
        sender.add_periodic_task(86400, clear_expired_exceptions.s())

    except Exception as e:
        if sentry:
            sentry.captureException()
        app.logger.error("[X] Scheduler Exception: {}".format(e))
        app.logger.error(traceback.format_exc())
        store_exception("scheduler", None, e)
