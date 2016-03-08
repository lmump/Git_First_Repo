#!/usr/bin/env python
'''
migrate a CCM release to an RTC stream
'''

import os, sys, re, tempfile, os.path
from pprint import *
from time import sleep

# configure logging before any RTC- or CCM-dependent modules
import logging as log
log.basicConfig(level=log.DEBUG, format='%(asctime)s %(message)s')

from rtc import *
from ccm import *

class mt_config:
	'container for configuration elements, specified externally'
	class ccm:
		pass
	class rtc:
		pass

def log_chdir(d):
	log.debug('entering "%s"' % d)
	os.chdir(d)

def execute(cmd):
	'general function for executing shell commands and capturing output'
	log.info('starting bash command: "%s"' % cmd)
	p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	o, e = p.communicate()
	assert p.returncode == 0, 'failure in subprocess command "%s"\nstdout:\n%sstderr:\n%s\n' % (cmd, o, e)
	log.info('back from bash command "%s"' % cmd)
	return o
	
def add_tasks(ccm_project, tasks, cli, work_item, rtc, project_rtc, work_area):
	'bring tasks into CCM working project, migrate to RTC stream'
	if not tasks:
		log.info('no tasks to add on top of current baseline "%s"' % ccm_project.baseline_project)
		#
		# migrate_task has the following side-effect; we need to do the same here,
		# even though are not migrating any tasks for this baseline.
		#
		log_chdir(mt_config.rtc.sandbox)
	for task in tasks:
		# skip excluded tasks, if any
		status = ccm_project._ccm.execute("query \"task('%s')\" -u -f %%status" % task)
		if re.compile(r'excluded').search(status):
			log.debug("skipping 'excluded' task '%s'" % task)
			continue

		# bring task into CCM working project.
		log.debug(ccm_project._ccm.execute("update_properties -recurse -add -tasks '%s' '%s'" % (task, ccm_project._spec),
										   ignore_err = r'(?ms)Failed to add any task.*cannot be changed'))
		tf = tempfile.NamedTemporaryFile()
		sleep(1.57)						# address odd behavior of find -newer/-cnewer, below
		updt = ccm_project.update()

		# FIXME
		# task_info could be fetched more efficiently:
		#  1. Issue a single "task -show info" instead of three and parse fields from output.
		#  2. Fetch task info on-demand, only if RTC determines that there are changes to check in.
		#     (pass a call-back function? lazy evaluation?)
		task_info = dict([('synopsis',    ccm_project._ccm.execute("task -show info '%s' -u -format '%%task_synopsis'" % task).strip()),
						  ('description', ccm_project._ccm.execute("task -show info '%s' -u -format '%%task_description'" % task)),
						  ('resolver',    ccm_project._ccm.execute("task -show resolver '%s'" % task).strip()),
						  ('cr_number',   ccm_project._ccm.execute("task -show info '%s' -u -format '%%cr_number'" % task).strip())])
		migrate_task(cli, task, work_item, rtc, task_info, project_rtc, epoch=tf, ccm_project=ccm_project)

def migrate_task(rtc_cli, task, work_item, rtc, task_info, project_rtc, epoch, ccm_project):
	'convert task objects into new change set and deliver'

	# determine what, if anything changed as a result of bringing in this task.
	#
	# FIXME: for some reason, "-cnewer" works better than "-newer" below.
	#        is this because the resolution on -newer is 1 second, or what?
	#        most likely this is due to CCM's subterfuge regarding file
	#        mod times in work areas...
	#
	log_chdir(mt_config.ccm.work_area)
	cpio = execute('find * ! -type d -cnewer "%s" | cpio -pdmuv "%s"'
				   % (epoch.name, mt_config.rtc.sandbox))
	log_chdir(mt_config.rtc.sandbox)
	if not re.compile(r'(?m)Unresolved:').search(rtc_cli.execute('status -w')):
		log.info('no changes in CCM task "%s", no RTC changeset will be created' % task)
		return

	# clean up any unicode bogosity in task_info strings
	for k, v in task_info.items():
		task_info[k] = v.decode(errors='replace')

	# save task object precedessors per CCM, since they are not 100% guaranteed to
	# be the same as what is in RTC at the time the current task is brought in.
	save_task_object_predecessors(ccm_project, task)

	# create new changeset.
	txt = rtc_cli.execute('checkin --delim-none .', scm_opts='-a n -u y')
	log.debug(txt)
	m = re.compile(r'(?m)Change sets:\W*\(([-_A-Za-z0-9]+)\)').search(txt)
	if m:
		csid = m.group(1)
	else:
		raise RTCError('unable to find changeset id in: \n%s' % txt)

	# set changeset comment to CCM task id.
	log.info(rtc_cli.execute('changeset comment "%s" "%s"' % (csid, task)))

	# associate changeset to common work item.
	log.info(rtc_cli.execute('changeset associate "%s" "%s"' % (csid, work_item)))

	# create new work item for task metadata.
	wi = WorkItem(rtc, project_rtc)
	wi.title(task_info['synopsis'])
	desc = '[resolver: "%s"]<p>%s\n' % (task_info['resolver'], task_info['description'])
	if re.compile(r'CSC[a-z]{2}\d{5}$').match(task_info['cr_number']):
		wi.cdets(task_info['cr_number'])
	else:
		desc = '[migrated from CM/Synergy]<p>[cr_number (invalid CDETS): "%s"]<p>%s\n' % (task_info['cr_number'], desc)
	wi.getset('dc:description', desc)
	wi.flush()
	log.info(rtc_cli.execute('changeset associate "%s" "%s"' % (csid, wi.id)))

	# deliver changeset
	log.info(rtc_cli.execute('deliver'))

def align_sandbox(rtc_cli, work_item, rtc, baseline):
	'''
	sync RTC sandbox with CCM work area.
	this handles the case where tasks are removed between baselines.
	'''
	execute('rsync -av --exclude="%s/" --exclude=.jazz5/ --exclude=.jazzShed/ --delete "%s/" "%s"'
			% (mt_config.rtc.ccm_versions, mt_config.ccm.work_area, mt_config.rtc.sandbox))
	log_chdir(mt_config.rtc.sandbox)
	if not re.compile(r'(?m)Unresolved:').search(rtc_cli.execute('status -w')):
		log.info('baselines match, no baseline alignment changeset will be created for "%s"' % baseline)
		return
	log.info('baseline alignment required for "%s"' % baseline)
	txt = rtc_cli.execute('checkin --delim-none .', scm_opts='-a n -u y')
	log.debug(txt)
	m = re.compile(r'(?m)Change sets:\W*\(([-_A-Za-z0-9]+)\)').search(txt)
	if m:
		csid = m.group(1)
	else:
		log.error('unable to find alignment changeset id in: "%s" (ignoring)' % txt)
		return

	# set changeset comment to CCM task id.
	log.info(rtc_cli.execute('changeset comment "%s" "baseline realignment: %s"' % (csid, baseline)))

	# associate changeset to common work item.
	log.info(rtc_cli.execute('changeset associate "%s" "%s"' % (csid, work_item)))

	# deliver changeset
	log.info(rtc_cli.execute('deliver'))

def save_task_object_predecessors(project, task):
	task_pred_dir = '%s/%s' % (mt_config.rtc.task_pred_dir, task)
	os.path.isdir(task_pred_dir) or os.makedirs(task_pred_dir)

	objects = project._ccm.execute("task -show objects -u '%s' -f '%%objectname'" % task)
	objects = project._ccm.text2list(objects)
	log.info('saving predecessor objects for task "%s" in "%s":\n%s' % (task, mt_config.rtc.ccm_versions, pformat(objects)))
	allpreds = list()
	for obj in objects:
		objpreds = project._ccm.execute_failok("query \"is_predecessor_of('%s')\" -u -f '%%objectname'" % obj)
		if objpreds:
			for pred in project._ccm.text2list(objpreds):
				allpreds.append(pred)

	for pred in allpreds:
		project._ccm.execute('cat "%s" > "%s/%s"' % (pred, task_pred_dir, pred))

def main():
	try:
		'simple command line argument extraction'
		if sys.argv[1] == "-i":
			baseline_advisor = raw_input
			sys.argv = sys.argv[0:1] + sys.argv[2:]
		else:
			baseline_advisor = log.info
		user, password, config = sys.argv[1], sys.argv[2], sys.argv[3]
	except IndexError:
		raise RTCError('usage: %s <user> <password> <config-file>' % sys.argv[0])

	'load configuration'
	execfile(config)
	mt_config.rtc.task_pred_dir = '%s/%s' % (mt_config.rtc.sandbox, mt_config.rtc.ccm_versions)

	log.info('configuration for this migration:\n%s\n%s' % (pformat(mt_config.ccm.__dict__), pformat(mt_config.rtc.__dict__)))

	rtc = RTC(host=mt_config.rtc.host, root=mt_config.rtc.root, user=user, password=password)
	cli = CLI(rtc.server, user, password)
	ccm = CCM(server=(mt_config.ccm.host, mt_config.ccm.db))

	working_project = Project(mt_config.ccm.project, ccm)
	log.info(pformat(working_project.__dict__))

	# find starting baseline
	baselines = working_project.baselines(purposes=mt_config.ccm.purposes)
	try:
		idx = baselines.index(mt_config.ccm.baseline_initial)
	except ValueError:
		raise CCMError("baseline '%s' not found in '%s'" % (mt_config.ccm.baseline_initial, mt_config.ccm.release))
	baselines = baselines[idx:]
	log.debug('migrating the following baselines:\n%s' % pformat(baselines))

	# align working project with initial baseline
	working_project.baseline_align(baselines[0])

	log.info('working project "%s" aligned at "%s"' % (mt_config.ccm.project, baselines[0]))

	# process baselines
	for idx in xrange(1, len(baselines)):
		current_bl, next_bl = baselines[idx-1], baselines[idx]

		baseline_advisor('''

In BASELINE loop
================================================================		
Migrating from "%s" to "%s"

Press Return to continue (Ctl-C to stop): ''' % (current_bl, next_bl))

		tasks2add, tasks2remove = ccm.baseline_compare(current_bl, next_bl, working_project.name)

		log.info('migrating to baseline "%s"' % next_bl)
		log.info('adding tasks:\n%s' % pformat(tasks2add))

		# migrate task-by-task
		add_tasks(ccm_project = working_project,
				  tasks=tasks2add,
				  cli=cli,
				  rtc=rtc,
				  project_rtc=mt_config.rtc.project,
				  work_item=mt_config.rtc.work_item,
				  work_area=mt_config.ccm.work_area)

		# create RTC baseline
		cli.create_snapshot(remove_dcm_prefix(next_bl), mt_config.rtc.workspace, mt_config.rtc.stream)

		# align work area with next baseline
		working_project.baseline_align(next_bl)

		# At this point, work_area and sandbox should be nearly
		# identical, both aligned with next_bl. However, if tasks were
		# removed from the previously migrated baseline, then it may
		# be necessary to make the same changes to their associated
		# objects from the RTC sandbox.
		align_sandbox(rtc_cli=cli, rtc=rtc, work_item=mt_config.rtc.work_item, baseline=next_bl)

	log.info('migration completed')


if __name__ == "__main__":
	main()

'''
Package as single executable using PyInstaller (I used v1.5.1):

$ python Configure.py
$ python Makespec.py --onefile path/to/ccm2rtc.py
$ python Build.py ccm2rtc/ccm2rtc.spec

Executable file: ccm2rtc/dist/ccm2rtc
'''

