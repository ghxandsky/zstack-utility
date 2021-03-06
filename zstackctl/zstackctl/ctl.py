#!/usr/bin/python

import argparse
import sys
import os
import subprocess
import signal
import getpass
import simplejson
from termcolor import colored
import ConfigParser
import StringIO
import functools
import time
import random
import string
from configobj import ConfigObj
import tempfile
import pwd, grp
import traceback
import uuid
import yaml

def signal_handler(signal, frame):
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def loop_until_timeout(timeout, interval=1):
    def wrap(f):
        @functools.wraps(f)
        def inner(*args, **kwargs):
            current_time = time.time()
            expired = current_time + timeout
            while current_time < expired:
                if f(*args, **kwargs):
                    return True
                time.sleep(interval)
                current_time = time.time()
            return False
        return inner
    return wrap

def find_process_by_cmdline(cmdlines):
    pids = [pid for pid in os.listdir('/proc') if pid.isdigit()]
    for pid in pids:
        try:
            with open(os.path.join('/proc', pid, 'cmdline'), 'r') as fd:
                cmdline = fd.read()

            is_find = True
            for c in cmdlines:
                if c not in cmdline:
                    is_find = False
                    break

            if not is_find:
                continue

            return pid
        except IOError:
            continue

    return None

def ssh_run_full(ip, cmd, params=[], pipe=True):
    remote_path = '/tmp/%s.sh' % uuid.uuid4()
    script = '''/bin/bash << EOF
cat << EOF1 > %s
%s
EOF1
/bin/bash %s %s
ret=$?
rm -f %s
exit $ret
EOF''' % (remote_path, cmd, remote_path, ' '.join(params), remote_path)

    scmd = ShellCmd('ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no  %s "%s"' % (ip, script), pipe=pipe)
    scmd(False)
    return scmd

def ssh_run(ip, cmd, params=[]):
    scmd = ssh_run_full(ip, cmd, params)
    if scmd.return_code != 0:
        scmd.raise_error()
    return scmd.stdout

def ssh_run_no_pipe(ip, cmd, params=[]):
    scmd = ssh_run_full(ip, cmd, params, False)
    if scmd.return_code != 0:
        scmd.raise_error()
    return scmd.stdout

class CtlError(Exception):
    pass

def warn(msg):
    sys.stdout.write('WARNING: %s\n' % msg)

def error(msg):
    sys.stderr.write(colored('ERROR: %s\n' % msg, 'red'))
    sys.exit(1)

def error_not_exit(msg):
    sys.stderr.write(colored('ERROR: %s\n' % msg, 'red'))

def info(*msg):
    if len(msg) == 1:
        out = '%s\n' % ''.join(msg)
    else:
        out = ''.join(msg)
    sys.stdout.write(out)

class ExceptionWrapper(object):
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        if globals().get('verbose', False) and exc_type and exc_val and exc_tb:
            error_not_exit(''.join(traceback.format_exception(exc_type, exc_val, exc_tb)))

        if exc_type == CtlError:
            return

        if exc_val:
            error('%s\n%s' % (str(exc_val), self.msg))

def on_error(msg):
    return ExceptionWrapper(msg)

def error_if_tool_is_missing(tool):
    if shell_return('which %s' % tool) != 0:
        raise CtlError('cannot find tool "%s", please install it and re-run' % tool)

def expand_path(path):
    if path.startswith('~'):
        return os.path.expanduser(path)
    else:
        return os.path.abspath(path)

class Ansible(object):
    def __init__(self, yaml, host='localhost', debug=False, ssh_key='none'):
        self.yaml = yaml
        self.host = host
        self.debug = debug
        self.ssh_key = ssh_key

    def __call__(self, *args, **kwargs):
        error_if_tool_is_missing('ansible-playbook')

        cmd = '''
yaml_file=`mktemp`
cat <<EOF >> $$yaml_file
$yaml
EOF

ansible_cmd="ansible-playbook $$yaml_file -i '$host,'"

if [ $debug -eq 1 ]; then
   ansible_cmd="$$ansible_cmd -vvvv"
fi

if [ "$ssh_key" != "none" ]; then
   ansible_cmd="$$ansible_cmd --private-key=$ssh_key"
   ssh -oPasswordAuthentication=no -oStrictHostKeyChecking=no -i $ssh_key $host 'echo hi > /dev/null'
else
   ssh -oPasswordAuthentication=no -oStrictHostKeyChecking=no $host 'echo hi > /dev/null'
fi

if [ $$? -ne 0 ]; then
   ansible_cmd="$$ansible_cmd --ask-pass"
fi

eval $$ansible_cmd
ret=$$?
rm -f $$yaml_file
exit $$ret
'''
        t = string.Template(cmd)
        cmd = t.substitute({
            'yaml': self.yaml,
            'host': self.host,
            'debug': int(self.debug),
            'ssh_key': self.ssh_key
        })

        with on_error('Ansible failure'):
            try:
                shell_no_pipe(cmd)
            except CtlError:
                raise Exception('see prior Ansible log for detailed information')

def ansible(yaml, host='localhost', debug=False, ssh_key=None):
    Ansible(yaml, host, debug, ssh_key or 'none')()

def check_zstack_user():
    try:
        pwd.getpwnam('zstack')
    except KeyError:
        raise CtlError('cannot find user account "zstack", your installation seems incomplete')

    try:
        grp.getgrnam('zstack')
    except KeyError:
        raise CtlError('cannot find user account "zstack", your installation seems incomplete')

class UseUserZstack(object):
    def __init__(self):
        self.root_uid = None
        self.root_gid = None
        check_zstack_user()

    def __enter__(self):
        self.root_uid = os.getuid()
        self.root_gid = os.getgid()
        self.root_home = os.environ['HOME']

        os.setegid(grp.getgrnam('zstack').gr_gid)
        os.seteuid(pwd.getpwnam('zstack').pw_uid)
        os.environ['HOME'] = os.path.expanduser('~zstack')

    def __exit__(self, exc_type, exc_val, exc_tb):
        os.seteuid(self.root_uid)
        os.setegid(self.root_gid)
        os.environ['HOME'] = self.root_home

def use_user_zstack():
    return UseUserZstack()

class PropertyFile(object):
    def __init__(self, path, use_zstack=True):
        self.path = path
        self.use_zstack = use_zstack
        if not os.path.isfile(self.path):
            raise CtlError('cannot find property file at %s' % self.path)

        with on_error("errors on reading %s" % self.path):
            self.config = ConfigObj(self.path, write_empty_values=True)

    def read_all_properties(self):
        with on_error("errors on reading %s" % self.path):
            return self.config.items()

    def delete_properties(self, keys):
        for k in keys:
            if k in self.config:
                del self.config[k]

        with use_user_zstack():
            self.config.write()

    def read_property(self, key):
        with on_error("errors on reading %s" % self.path):
            return self.config.get(key, None)

    def write_property(self, key, value):
        with on_error("errors on writing (%s=%s) to %s" % (key, value, self.path)):
            if self.use_zstack:
                with use_user_zstack():
                    self.config[key] = value
                    self.config.write()
            else:
                self.config[key] = value
                self.config.write()

    def write_properties(self, lst):
        with on_error("errors on writing list of key-value%s to %s" % (lst, self.path)):
            if self.use_zstack:
                with use_user_zstack():
                    for key, value in lst:
                        self.config[key] = value
                        self.config.write()
            else:
                for key, value in lst:
                    self.config[key] = value
                    self.config.write()

class CtlParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write('error:%s\n' % message)
        self.print_help()
        sys.exit(1)

class Ctl(object):
    DEFAULT_ZSTACK_HOME = '/usr/local/zstack/apache-tomcat/webapps/zstack/'
    USER_ZSTACK_HOME_DIR = os.path.expanduser('~zstack')

    def __init__(self):
        self.commands = {}
        self.command_list = []
        self.main_parser = CtlParser(prog='zstackctl', description="ZStack management tool", formatter_class=argparse.RawTextHelpFormatter)
        self.main_parser.add_argument('-v', help="verbose, print execution details", dest="verbose", action="store_true", default=False)
        self.zstack_home = None
        self.properties_file_path = None
        self.verbose = False
        self.extra_arguments = None

    def register_command(self, cmd):
        assert cmd.name, "command name cannot be None"
        assert cmd.description, "command description cannot be None"
        self.commands[cmd.name] = cmd
        self.command_list.append(cmd)

    def _locate_zstack_home(self):
        env_path = os.path.expanduser(SetEnvironmentVariableCmd.PATH)
        if os.path.isfile(env_path):
            env = PropertyFile(env_path)
            self.zstack_home = env.read_property('ZSTACK_HOME')

        if not self.zstack_home:
            self.zstack_home = os.environ.get('ZSTACK_HOME', None)

        if not self.zstack_home:
            warn('ZSTACK_HOME is not set, default to %s' % self.DEFAULT_ZSTACK_HOME)
            self.zstack_home = self.DEFAULT_ZSTACK_HOME

        if not os.path.isdir(self.zstack_home):
            raise CtlError('cannot find ZSTACK_HOME at %s, please set it in .bashrc or use zstack-ctl setenv ZSTACK_HOME=path' % self.zstack_home)

        os.environ['ZSTACK_HOME'] = self.zstack_home
        self.properties_file_path = os.path.join(self.zstack_home, 'WEB-INF/classes/zstack.properties')
        if not os.path.isfile(self.properties_file_path):
            warn('cannot find %s, your ZStack installation may have crashed' % self.properties_file_path)

    def get_env(self, name):
        env = PropertyFile(SetEnvironmentVariableCmd.PATH)
        return env.read_property(name)

    def put_envs(self, vs):
        if not os.path.exists(SetEnvironmentVariableCmd.PATH):
            shell('su - zstack -c "mkdir -p %s"' % os.path.dirname(SetEnvironmentVariableCmd.PATH))
            shell('su - zstack -c "touch %s"' % SetEnvironmentVariableCmd.PATH)
        env = PropertyFile(SetEnvironmentVariableCmd.PATH)
        env.write_properties(vs)

    def run(self):
        if os.getuid() != 0:
            raise CtlError('zstack-ctl needs root privilege, please run with sudo')

        subparsers = self.main_parser.add_subparsers(help="All sub-commands", dest="sub_command_name")
        for cmd in self.command_list:
            cmd.install_argparse_arguments(subparsers.add_parser(cmd.name, help=cmd.description + '\n\n'))

        args, self.extra_arguments = self.main_parser.parse_known_args(sys.argv[1:])
        self.verbose = args.verbose
        globals()['verbose'] = self.verbose

        cmd = self.commands[args.sub_command_name]

        if cmd.need_zstack_home():
            self._locate_zstack_home()

        if cmd.need_zstack_user():
            check_zstack_user()

        cmd(args)

    def internal_run(self, cmd_name, args=''):
        cmd = self.commands[cmd_name]
        assert cmd, 'cannot find command %s' % cmd_name

        params = [cmd_name]
        params.extend(args.split())
        args_obj, _ = self.main_parser.parse_known_args(params)

        if cmd.need_zstack_home():
            self._locate_zstack_home()

        if cmd.need_zstack_user():
            check_zstack_user()

        cmd(args_obj)

    def read_property_list(self, key):
        prop = PropertyFile(self.properties_file_path)
        ret = []
        for name, value in prop.read_all_properties():
            if name.startswith(key):
                ret.append((name, value))
        return ret

    def read_all_properties(self):
        prop = PropertyFile(self.properties_file_path)
        return prop.read_all_properties()

    def read_property(self, key):
        prop = PropertyFile(self.properties_file_path)
        return prop.read_property(key)

    def write_properties(self, properties):
        prop = PropertyFile(self.properties_file_path)
        with on_error('property must be in format of "key=value", no space before and after "="'):
            prop.write_properties(properties)

    def write_property(self, key, value):
        prop = PropertyFile(self.properties_file_path)
    def get_db_url(self):
        db_url = self.read_property("DB.url")
        if not db_url:
            db_url = self.read_property('DbFacadeDataSource.jdbcUrl')
        if not db_url:
            raise CtlError("cannot find DB url in %s. please set DB.url" % self.properties_file_path)
        return db_url

    def get_database_portal(self):
        db_user = self.read_property("DB.user")
        if not db_user:
            db_user = self.read_property('DbFacadeDataSource.user')
        if not db_user:
            raise CtlError("cannot find DB user in %s. please set DB.user" % self.properties_file_path)

        db_password = self.read_property("DB.password")
        if db_password is None:
            db_password = self.read_property('DbFacadeDataSource.password')

        if db_password is None:
            raise CtlError("cannot find DB password in %s. please set DB.password" % self.properties_file_path)

        db_url = self.get_db_url()
        db_hostname, db_port = db_url.lstrip('jdbc:mysql:').lstrip('/').split('/')[0].split(':')

        return db_hostname, db_port, db_user, db_password

    def check_if_management_node_has_stopped(self, force=False):
        db_hostname, db_port, db_user, db_password = self.get_database_portal()

        def get_nodes():
            query = MySqlCommandLineQuery()
            query.user = db_user
            query.password = db_password
            query.host = db_hostname
            query.port = db_port
            query.table = 'zstack'
            query.sql = 'select hostname,heartBeat from ManagementNodeVO'

            return query.query()

        def check():
            nodes = get_nodes()
            if nodes:
                node_ips = [n['hostname'] for n in nodes]
                raise CtlError('there are some management nodes%s are still running. Please stop all of them before performing the database upgrade.'
                               'If you are sure they have stopped, use option --force and run this command again.\n'
                               'If you are upgrade by all in on installer, use option -F and run all in one installer again.\n'
                               'WARNING: the database may crash if you run this command with --force but without stopping management nodes' % node_ips)

        def bypass_check():
            nodes = get_nodes()
            if nodes:
                node_ips = [n['hostname'] for n in nodes]
                info("it seems some nodes%s are still running. As you have specified option --force, let's wait for 10s to make sure those are stale records. Please be patient." % node_ips)
                time.sleep(10)
                new_nodes = get_nodes()

                for n in new_nodes:
                    for o in nodes:
                        if o['hostname'] == n['hostname'] and o['heartBeat'] != n['heartBeat']:
                            raise CtlError("node[%s] is still Running! Its heart-beat changed from %s to %s in last 10s. Please make sure you really stop it" %
                                           (n['hostname'], o['heartBeat'], n['heartBeat']))

        if force:
            bypass_check()
        else:
            check()

ctl = Ctl()

def script(cmd, args=None, no_pipe=False):
    if args:
        t = string.Template(cmd)
        cmd = t.substitute(args)

    fd, script_path = tempfile.mkstemp(suffix='.sh')
    os.fdopen(fd, 'w').write(cmd)
    try:
        if ctl.verbose:
            info('execute script:\n%s\n' % cmd)
        if no_pipe:
            shell_no_pipe('bash %s' % script_path)
        else:
            shell('bash %s' % script_path)
    finally:
        os.remove(script_path)

class ShellCmd(object):
    def __init__(self, cmd, workdir=None, pipe=True):
        self.cmd = cmd
        if pipe:
            self.process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.PIPE, cwd=workdir)
        else:
            self.process = subprocess.Popen(cmd, shell=True, cwd=workdir)

        self.return_code = None
        self.stdout = None
        self.stderr = None

    def raise_error(self):
        err = []
        err.append('failed to execute shell command: %s' % self.cmd)
        err.append('return code: %s' % self.process.returncode)
        err.append('stdout: %s' % self.stdout)
        err.append('stderr: %s' % self.stderr)
        raise CtlError('\n'.join(err))

    def __call__(self, is_exception=True):
        if ctl.verbose:
            info('executing shell command[%s]:' % self.cmd)

        (self.stdout, self.stderr) = self.process.communicate()
        if is_exception and self.process.returncode != 0:
            self.raise_error()

        self.return_code = self.process.returncode

        if ctl.verbose:
            info(simplejson.dumps({
                "shell" : self.cmd,
                "return_code" : self.return_code,
                "stdout": self.stdout,
                "stderr": self.stderr
            }, ensure_ascii=True, sort_keys=True, indent=4))

        return self.stdout

def shell(cmd, is_exception=True):
    return ShellCmd(cmd)(is_exception)

def shell_no_pipe(cmd, is_exception=True):
    return ShellCmd(cmd, pipe=False)(is_exception)

def shell_return(cmd):
    scmd = ShellCmd(cmd)
    scmd(False)
    return scmd.return_code

class Command(object):
    def __init__(self):
        self.name = None
        self.description = None
        self.cleanup_routines = []

    def install_argparse_arguments(self, parser):
        pass

    def install_cleanup_routine(self, func):
        self.cleanup_routines.append(func)

    def need_zstack_home(self):
        return True

    def need_zstack_user(self):
        return True

    def __call__(self, *args, **kwargs):
        try:
            self.run(*args)
        finally:
            for c in self.cleanup_routines:
                c()

    def run(self, args):
        raise CtlError('the command is not implemented')

def create_check_mgmt_node_command(timeout=10):
    USE_CURL = 0
    USE_WGET = 1
    NO_TOOL = 2

    def use_tool():
        cmd = ShellCmd('which wget')
        cmd(False)
        if cmd.return_code == 0:
            return USE_WGET
        else:
            cmd = ShellCmd('which curl')
            cmd(False)
            if cmd.return_code == 0:
                return USE_CURL
            else:
                return NO_TOOL

    what_tool = use_tool()
    if what_tool == USE_CURL:
        return ShellCmd('''curl --noproxy --connect-timeout 1 --retry %s --retry-delay 0 --retry-max-time %s --max-time %s -H "Content-Type: application/json" -d '{"org.zstack.header.apimediator.APIIsReadyToGoMsg": {}}' http://127.0.0.1:8080/zstack/api''' % (timeout, timeout, timeout))
    elif what_tool == USE_WGET:
        return ShellCmd('''wget --no-proxy -O- --tries=%s --timeout=1  --header=Content-Type:application/json --post-data='{"org.zstack.header.apimediator.APIIsReadyToGoMsg": {}}' http://127.0.0.1:8080/zstack/api''' % timeout)
    else:
        return None

def find_process_by_cmdline(keyword):
    pids = [pid for pid in os.listdir('/proc') if pid.isdigit()]
    for pid in pids:
        try:
            with open(os.path.join('/proc', pid, 'cmdline'), 'r') as fd:
                cmdline = fd.read()

            if keyword not in cmdline:
                continue

            return pid
        except IOError:
            continue

    return None

class MySqlCommandLineQuery(object):
    def __init__(self):
        self.user = None
        self.password = None
        self.host = 'localhost'
        self.port = 3306
        self.sql = None
        self.table = None

    def query(self):
        assert self.user, 'user cannot be None'
        assert self.sql, 'sql cannot be None'
        assert self.table, 'table cannot be None'

        sql = "%s\G" % self.sql
        if self.password:
            cmd = '''mysql -u %s -p%s --host %s --port %s -t %s -e "%s"''' % (self.user, self.password, self.host,
                                                                               self.port, self.table, sql)
        else:
            cmd = '''mysql -u %s --host %s --port %s -t %s -e "%s"''' % (self.user, self.host, self.port, self.table, sql)

        output = shell(cmd)
        output = output.strip(' \t\n\r')
        ret = []
        if not output:
            return ret

        current = None
        for l in output.split('\n'):
            if current is None and not l.startswith('*********'):
                raise CtlError('cannot parse mysql output generated by the sql "%s", output:\n%s' % (self.sql, output))

            if l.startswith('*********'):
                if current:
                    ret.append(current)
                current = {}
            else:
                l = l.strip()
                key, value = l.split(':', 1)
                current[key.strip()] = value[1:]

        if current:
            ret.append(current)

        return ret

class ShowStatusCmd(Command):
    def __init__(self):
        super(ShowStatusCmd, self).__init__()
        self.name = 'status'
        self.description = 'show ZStack status and information.'
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help='SSH URL, for example, root@192.168.0.10, to show the management node status on a remote machine')

    def _stop_remote(self, args):
        shell_no_pipe('ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no  %s "/usr/bin/zstack-ctl status"' % args.host)

    def run(self, args):
        if args.host:
            self._stop_remote(args)
            return

        log_path = os.path.join(ctl.zstack_home, "../../logs/management-server.log")
        log_path = os.path.normpath(log_path)

        info_list = [
            "ZSTACK_HOME: %s" % ctl.zstack_home,
            "zstack.properties: %s" % ctl.properties_file_path,
            "log4j2.xml: %s" % os.path.join(os.path.dirname(ctl.properties_file_path), 'log4j2.xml'),
            "PID file: %s" % os.path.join(os.path.expanduser('~zstack'), "management-server.pid"),
            "log file: %s" % log_path
        ]


        def check_zstack_status():
            cmd = create_check_mgmt_node_command()

            def write_status(status):
                info_list.append('status: %s' % status)

            if not cmd:
                write_status('cannot detect status, no wget and curl installed')
                return

            cmd(False)
            if cmd.return_code != 0:
                pid = get_management_node_pid()
                if pid:
                    write_status('%s, the management node seems to become zombie as it stops responding APIs but the '
                                 'process(PID: %s) is still running. Please stop the node using zstack-ctl stop_node' %
                                 (colored('Unknown', 'yellow'), pid))
                else:
                    write_status(colored('Stopped', 'red'))
                return

            if 'false' in cmd.stdout:
                write_status('Starting, should be ready in a few seconds')
            elif 'true' in cmd.stdout:
                write_status(colored('Running', 'green'))
            else:
                write_status('Unknown')

        def show_version():
            db_hostname, db_port, db_user, db_password = ctl.get_database_portal()
            if db_password:
                cmd = ShellCmd('''mysql -u %s -p%s --host %s --port %s -t zstack -e "show tables like 'schema_version'"''' %
                            (db_user, db_password, db_hostname, db_port))
            else:
                cmd = ShellCmd('''mysql -u %s --host %s --port %s -t zstack -e "show tables like 'schema_version'"''' %
                            (db_user, db_hostname, db_port))

            cmd(False)
            if cmd.return_code != 0:
                info_list.append('version: %s' % colored('unknown, MySQL is not running', 'yellow'))
                return

            out = cmd.stdout
            if 'schema_version' not in out:
                version = '0.6'
            else:
                query = MySqlCommandLineQuery()
                query.host = db_hostname
                query.port = db_port
                query.user = db_user
                query.password = db_password
                query.table = 'zstack'
                query.sql = "select version from schema_version order by version desc"
                ret = query.query()

                v = ret[0]
                version = v['version']

            info_list.append('version: %s' % version)

        check_zstack_status()
        show_version()

        info('\n'.join(info_list))

class DeployDBCmd(Command):
    DEPLOY_DB_SCRIPT_PATH = "WEB-INF/classes/deploydb.sh"
    ZSTACK_PROPERTY_FILE = "WEB-INF/classes/zstack.properties"

    def __init__(self):
        super(DeployDBCmd, self).__init__()
        self.name = "deploydb"
        self.description = (
            "deploy a new ZStack database, create a user 'zstack' with password specified in '--zstack-password',\n"
            "and update zstack.properties if --no-update is not set.\n"
            "\nDANGER: this will erase the existing ZStack database.\n"
            "NOTE: If the database is running on a remote host, please make sure you have granted privileges to the root user by:\n"
            "\n\tGRANT ALL PRIVILEGES ON *.* TO 'root'@'%%' IDENTIFIED BY 'your_root_password' WITH GRANT OPTION;\n"
            "\tFLUSH PRIVILEGES;\n"
        )
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--root-password', help='root user password of MySQL. [DEFAULT] empty password')
        parser.add_argument('--zstack-password', help='password of user "zstack". [DEFAULT] empty password')
        parser.add_argument('--host', help='IP or DNS name of MySQL host; default is localhost', default='localhost')
        parser.add_argument('--port', help='port of MySQL host; default is 3306', type=int, default=3306)
        parser.add_argument('--no-update', help='do NOT update database information to zstack.properties; if you do not know what this means, do not use it', action='store_true', default=False)
        parser.add_argument('--drop', help='drop existing zstack database', action='store_true', default=False)
        parser.add_argument('--keep-db', help='keep existing zstack database and not raise error.', action='store_true', default=False)

    def run(self, args):
        error_if_tool_is_missing('mysql')

        script_path = os.path.join(ctl.zstack_home, self.DEPLOY_DB_SCRIPT_PATH)
        if not os.path.exists(script_path):
            error('cannot find %s, your ZStack installation may have been corrupted, please reinstall it' % script_path)

        property_file_path = os.path.join(ctl.zstack_home, self.ZSTACK_PROPERTY_FILE)
        if not os.path.exists(property_file_path):
            error('cannot find %s, your ZStack installation may have been corrupted, please reinstall it' % property_file_path)

        if args.root_password:
            check_existing_db = 'mysql --user=root --password=%s --host=%s --port=%s -e "use zstack"' % (args.root_password, args.host, args.port)
        else:
            check_existing_db = 'mysql --user=root --host=%s --port=%s -e "use zstack"' % (args.host, args.port)

        cmd = ShellCmd(check_existing_db)
        cmd(False)
        if not args.root_password:
            args.root_password = "''"
        if not args.zstack_password:
            args.zstack_password = "''"

        if cmd.return_code == 0 and not args.drop:
            if args.keep_db:
                info('detected existing zstack database and keep it; if you want to drop it, please append parameter --drop, instead of --keep-db\n')
            else:
                raise CtlError('detected existing zstack database; if you are sure to drop it, please append parameter --drop')
        else:
            cmd = ShellCmd('bash %s root %s %s %s %s' % (script_path, args.root_password, args.host, args.port, args.zstack_password))
            cmd(False)
            if cmd.return_code != 0:
                if ('ERROR 1044' in cmd.stdout or 'ERROR 1044' in cmd.stderr) or ('Access denied' in cmd.stdout or 'Access denied' in cmd.stderr):
                    raise CtlError(
                        "failed to deploy database, access denied; if your root password is correct and you use IP rather than localhost,"
                        "it's probably caused by the privileges are not granted to root user for remote access; please see instructions in 'zstack-ctl -h'."
                        "error details: %s, %s\n" % (cmd.stdout, cmd.stderr)
                    )
                else:
                    cmd.raise_error()

        if not args.no_update:
            if args.zstack_password == "''":
                args.zstack_password = ''

            properties = [
                ("DB.user", "zstack"),
                ("DB.password", args.zstack_password),
                ("DB.url", 'jdbc:mysql://%s:%s' % (args.host, args.port)),
            ]

            ctl.write_properties(properties)

        info('Successfully deployed ZStack database and updated corresponding DB information in %s' % property_file_path)

class TailLogCmd(Command):
    def __init__(self):
        super(TailLogCmd, self).__init__()
        self.name = 'taillog'
        self.description = "shortcut to print management node log to stdout"
        ctl.register_command(self)

    def run(self, args):
        log_path = os.path.join(ctl.zstack_home, "../../logs/management-server.log")
        log_path = os.path.normpath(log_path)
        if not os.path.isfile(log_path):
            raise CtlError('cannot find %s' % log_path)

        script = ShellCmd('tail -f %s' % log_path, pipe=False)
        script()

class ConfigureCmd(Command):
    def __init__(self):
        super(ConfigureCmd, self).__init__()
        self.name = 'configure'
        self.description = "configure zstack.properties"
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help='SSH URL, for example, root@192.168.0.10, to set properties in zstack.properties on the remote machine')
        parser.add_argument('--duplicate-to-remote', help='SSH URL, for example, root@192.168.0.10, to copy zstack.properties on this machine to the remote machine')
        parser.add_argument('--use-file', help='path to a file that will be used to as zstack.properties')

    def _configure_remote_node(self, args):
        shell_no_pipe('ssh %s "/usr/bin/zstack-ctl configure %s"' % (args.host, ' '.join(ctl.extra_arguments)))

    def _duplicate_remote_node(self, args):
        tmp_file_name = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        tmp_file_name = os.path.join('/tmp/', tmp_file_name)
        with open(ctl.properties_file_path, 'r') as fd:
            txt = fd.read()
            cmd = '''ssh -T %s << EOF
cat <<EOT > %s
%s
EOT

if [ $? != 0 ]; then
    print "cannot create temporary properties file"
    exit 1
fi

/usr/bin/zstack-ctl configure --use-file %s
ret=$?
rm -f %s
exit $ret
EOF
'''
            shell_no_pipe(cmd % (args.duplicate_to_remote, tmp_file_name, txt, tmp_file_name, tmp_file_name))
            info("successfully copied %s to remote machine %s" % (ctl.properties_file_path, args.duplicate_to_remote))

    def _use_file(self, args):
        path = os.path.expanduser(args.use_file)
        if not os.path.isfile(path):
            raise CtlError('cannot find file %s' % path)

        shell('cp -f %s %s' % (path, ctl.properties_file_path))

    def run(self, args):
        if args.use_file:
            self._use_file(args)
            return

        if args.duplicate_to_remote:
            self._duplicate_remote_node(args)
            return

        if not ctl.extra_arguments:
            raise CtlError('please input properties that are in format of "key=value" split by space')

        if args.host:
            self._configure_remote_node(args)
            return

        properties = [l.split('=', 1) for l in ctl.extra_arguments]
        ctl.write_properties(properties)

def get_management_node_pid():
    DEFAULT_PID_FILE_PATH = os.path.join(os.path.expanduser('~zstack'), "management-server.pid")

    pid = find_process_by_cmdline('appName=zstack')
    if pid:
        return pid

    pid_file_path = ctl.read_property('pidFilePath')
    if not pid_file_path:
        pid_file_path = DEFAULT_PID_FILE_PATH

    if not os.path.exists(pid_file_path):
        return None

    with open(pid_file_path, 'r') as fd:
        pid = fd.read()
        try:
            pid = int(pid)
            proc_pid = '/proc/%s' % pid
            if os.path.exists(proc_pid):
                return pid
        except Exception:
            return None

    return None

class StopAllCmd(Command):
    def __init__(self):
        super(StopAllCmd, self).__init__()
        self.name = 'stop'
        self.description = 'stop all ZStack related services including cassandra, kairosdb, zstack management node, web UI' \
                           ' if those services are installed'
        ctl.register_command(self)

    def run(self, args):
        def stop_cassandra():
            exe = ctl.get_env(InstallCassandraCmd.CASSANDRA_EXEC)
            if not exe or not os.path.exists(exe):
                info('skip stopping cassandra, it is not installed')
                return

            info(colored('Stopping cassandra, it may take a few minutes...', 'blue'))
            ctl.internal_run('cassandra', '--stop')

        def stop_kairosdb():
            exe = ctl.get_env(InstallKairosdbCmd.KAIROSDB_EXEC)
            if not exe or not os.path.exists(exe):
                info('skip stopping kairosdb, it is not installed')
                return

            info(colored('Stopping kairosdb, it may take a few minutes...', 'blue'))
            ctl.internal_run('kairosdb', '--stop')

        def stop_mgmt_node():
            info(colored('Stopping ZStack management node, it may take a few minutes...', 'blue'))
            ctl.internal_run('stop_node')

        def stop_ui():
            virtualenv = '/var/lib/zstack/virtualenv/zstack-dashboard'
            if not os.path.exists(virtualenv):
                info('skip stopping web UI, it is not installed')
                return

            info(colored('Stopping ZStack web UI, it may take a few minutes...', 'blue'))
            ctl.internal_run('stop_ui')

        stop_ui()
        stop_mgmt_node()
        stop_kairosdb()
        stop_cassandra()

class StartAllCmd(Command):

    def __init__(self):
        super(StartAllCmd, self).__init__()
        self.name = 'start'
        self.description = 'start all ZStack related services including cassandra, kairosdb, zstack management node, web UI' \
                           ' if those services are installed'
        ctl.register_command(self)

    def run(self, args):
        def start_cassandra():
            exe = ctl.get_env(InstallCassandraCmd.CASSANDRA_EXEC)
            if not exe or not os.path.exists(exe):
                info('skip starting cassandra, it is not installed')
                return

            info(colored('Starting cassandra, it may take a few minutes...', 'blue'))
            ctl.internal_run('cassandra', '--start --wait-timeout 120')

        def start_kairosdb():
            exe = ctl.get_env(InstallKairosdbCmd.KAIROSDB_EXEC)
            if not exe or not os.path.exists(exe):
                info('skip starting kairosdb, it is not installed')
                return

            info(colored('Starting kairosdb, it may take a few minutes...', 'blue'))
            ctl.internal_run('kairosdb', '--start --wait-timeout 120')

        def start_mgmt_node():
            info(colored('Starting ZStack management node, it may take a few minutes...', 'blue'))
            ctl.internal_run('start_node')

        def start_ui():
            virtualenv = '/var/lib/zstack/virtualenv/zstack-dashboard'
            if not os.path.exists(virtualenv):
                info('skip starting web UI, it is not installed')
                return

            info(colored('Starting ZStack web UI, it may take a few minutes...', 'blue'))
            ctl.internal_run('start_ui')

        start_cassandra()
        start_kairosdb()
        start_mgmt_node()
        start_ui()

class StartCmd(Command):
    START_SCRIPT = '../../bin/startup.sh'
    SET_ENV_SCRIPT = '../../bin/setenv.sh'

    def __init__(self):
        super(StartCmd, self).__init__()
        self.name = 'start_node'
        self.description = 'start the ZStack management node on this machine'
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help='SSH URL, for example, root@192.168.0.10, to start the management node on a remote machine')
        parser.add_argument('--timeout', help='Wait for ZStack Server startup timeout, default is 300 seconds.', default=300)

    def _start_remote(self, args):
        info('it may take a while because zstack-ctl will wait for management node ready to serve API')
        shell_no_pipe('ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no  %s "/usr/bin/zstack-ctl start_node --timeout=%s"' % (args.host, args.timeout))

    def run(self, args):
        if args.host:
            self._start_remote(args)
            return

        # clean the error log before booting
        boot_error_log = os.path.join(ctl.USER_ZSTACK_HOME_DIR, 'bootError.log')
        shell('rm -f %s' % boot_error_log)

        pid = get_management_node_pid()
        if pid:
            info('the management node[pid:%s] is already running' % pid)
            return

        def check_ip_port(host, port):
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex((host, int(port)))
            return result == 0

        def check_8080():
            if shell_return('netstat -nap | grep :8080 | grep LISTEN > /dev/null') == 0:
                raise CtlError('8080 is occupied by some process. Please use netstat to find out and stop it')

        def check_msyql():
            db_hostname, db_port, db_user, db_password = ctl.get_database_portal()

            if not check_ip_port(db_hostname, db_port):
                raise CtlError('unable to connect to %s:%s, please check if the MySQL is running and the firewall rules' % (db_hostname, db_port))

            with on_error('unable to connect to MySQL'):
                shell('mysql --host=%s --user=%s --password=%s --port=%s -e "select 1"' % (db_hostname, db_user, db_password, db_port))

        def check_rabbitmq():
            RABBIT_PORT = 5672

            with on_error('unable to get RabbitMQ server IPs from %s, please check CloudBus.serverIp.0'):
                ips = ctl.read_property_list('CloudBus.serverIp.')
                if not ips:
                    raise CtlError('no RabbitMQ IPs defined in %s, please specify it use CloudBus.serverIp.0=the_ip' % ctl.properties_file_path)

                for key, ip in ips:
                    if not check_ip_port(ip, RABBIT_PORT):
                        raise CtlError('cannot connect to RabbitMQ server[ip:%s, port:%s] defined by item[%s] in %s' % (ip, RABBIT_PORT, key, ctl.properties_file_path))

        def prepare_setenv():
            setenv_path = os.path.join(ctl.zstack_home, self.SET_ENV_SCRIPT)
            catalina_opts = [
                '-Djava.net.preferIPv4Stack=true',
                '-Dcom.sun.management.jmxremote=true',
                '-Djava.security.egd=file:/dev/./urandom'
            ]

            co = ctl.get_env('CATALINA_OPTS')
            if co:
                info('use CATALINA_OPTS[%s] set in environment zstack environment variables; check out them by "zstack-ctl getenv"' % co)
                catalina_opts.extend(co.split(' '))

            with open(setenv_path, 'w') as fd:
                fd.write('export CATALINA_OPTS=" %s"' % ' '.join(catalina_opts))

        def start_mgmt_node():
            shell('sudo -u zstack sh %s -DappName=zstack' % os.path.join(ctl.zstack_home, self.START_SCRIPT))
            info("successfully started Tomcat container; now it's waiting for the management node ready for serving APIs, which may take a few seconds")

        def wait_mgmt_node_start():
            log_path = os.path.join(ctl.zstack_home, "../../logs/management-server.log")
            timeout = int(args.timeout)
            @loop_until_timeout(timeout)
            def check():
                if os.path.exists(boot_error_log):
                    with open(boot_error_log, 'r') as fd:
                        raise CtlError('the management server fails to boot; details can be found in the log[%s],'
                                       'here is a brief of the error:\n%s' % (log_path, fd.read()))

                cmd = create_check_mgmt_node_command(1)
                cmd(False)
                return cmd.return_code == 0

            if not check():
                raise CtlError('no management-node-ready message received within %s seconds, please check error in log file %s' % (timeout, log_path))

        user = getpass.getuser()
        if user != 'root':
            raise CtlError('please use sudo or root user')

        check_8080()
        check_msyql()
        check_rabbitmq()
        prepare_setenv()
        start_mgmt_node()
        #sleep a while, since zstack won't start up so quick
        time.sleep(5)

        try:
            wait_mgmt_node_start()
        except CtlError as e:
            try:
                info("the management node failed to start, stop it now ...")
                ctl.internal_run('stop_node')
            except:
                pass

            raise e

        info('successfully started management node')

class StopCmd(Command):
    STOP_SCRIPT = "../../bin/shutdown.sh"

    def __init__(self):
        super(StopCmd, self).__init__()
        self.name = 'stop_node'
        self.description = 'stop the ZStack management node on this machine'
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help='SSH URL, for example, root@192.168.0.10, to stop the management node on a remote machine')
        parser.add_argument('--force', '-f', help='force kill the java process, without waiting.', action="store_true", default=False)

    def _stop_remote(self, args):
        if args.force:
            shell_no_pipe('ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no %s "/usr/bin/zstack-ctl stop_node --force"' % args.host)
        else:
            shell_no_pipe('ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no %s "/usr/bin/zstack-ctl stop_node"' % args.host)

    def run(self, args):
        if args.host:
            self._stop_remote(args)
            return

        pid = get_management_node_pid()
        if not pid:
            info('the management node has been stopped')
            return

        timeout = 30
        if not args.force:
            @loop_until_timeout(timeout)
            def wait_stop():
                return get_management_node_pid() is None

            shell('bash %s' % os.path.join(ctl.zstack_home, self.STOP_SCRIPT))
            if wait_stop():
                info('successfully stopped management node')
                return

        pid = get_management_node_pid()
        if pid:
            if not args.force:
                info('unable to stop management node within %s seconds, kill it' % timeout)
            with on_error('unable to kill -9 %s' % pid):
                shell('kill -9 %s' % pid)

class SaveConfigCmd(Command):
    DEFAULT_PATH = '~/.zstack/'

    def __init__(self):
        super(SaveConfigCmd, self).__init__()
        self.name = 'save_config'
        self.description = 'save ZStack configuration from ZSTACK_HOME to specified folder'
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--save-to', help='the folder where ZStack configurations should be saved')

    def run(self, args):
        path = args.save_to
        if not path:
            path = self.DEFAULT_PATH

        path = os.path.expanduser(path)
        if not os.path.exists(path):
            os.makedirs(path)

        properties_file_path = os.path.join(path, 'zstack.properties')
        shell('yes | cp %s %s' % (ctl.properties_file_path, properties_file_path))
        info('successfully saved %s to %s' % (ctl.properties_file_path, properties_file_path))

class RestoreConfigCmd(Command):
    DEFAULT_PATH = '~/.zstack/'

    def __init__(self):
        super(RestoreConfigCmd, self).__init__()
        self.name = "restore_config"
        self.description = 'restore ZStack configuration from specified folder to ZSTACK_HOME'
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--restore-from', help='the folder where ZStack configurations should be found')

    def run(self, args):
        path = args.restore_from
        if not path:
            path = self.DEFAULT_PATH

        path = os.path.expanduser(path)
        if os.path.isdir(path):
            properties_file_path = os.path.join(path, 'zstack.properties')
        elif os.path.isfile(path):
            properties_file_path = path
        else:
            raise CtlError('cannot find zstack.properties at %s' % path)

        shell('yes | cp %s %s' % (properties_file_path, ctl.properties_file_path))
        info('successfully restored zstack.properties from %s to %s' % (properties_file_path, ctl.properties_file_path))

class InstallDbCmd(Command):
    def __init__(self):
        super(InstallDbCmd, self).__init__()
        self.name = "install_db"
        self.description = (
            "install MySQL database on a target machine which can be a remote machine or the local machine."
            "\nNOTE: you may need to set --login-password to password of previous MySQL root user, if the machine used to have MySQL installed and removed."
            "\nNOTE: if you hasn't setup public key for ROOT user on the remote machine, this command will prompt you for password of SSH ROOT user for the remote machine."
        )
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help='host IP, for example, 192.168.0.212, please specify the real IP rather than "localhost" or "127.0.0.1" when installing on local machine; otherwise management nodes on other machines cannot access the DB.', required=True)
        parser.add_argument('--root-password', help="new password of MySQL root user; an empty password is used if both this option and --login-password option are omitted")
        parser.add_argument('--login-password', help="login password of MySQL root user; an empty password is used if this option is omitted."
                            "\n[NOTE] this option is needed only when the machine has MySQL previously installed and removed; the old MySQL root password will be left in the system,"
                            "you need to input it in order to reset root password for the new installed MySQL.", default=None)
        parser.add_argument('--debug', help="open Ansible debug option", action="store_true", default=False)
        parser.add_argument('--yum', help="Use ZStack predefined yum repositories. The valid options include: alibase,aliepel,163base,ustcepel,zstack-local. NOTE: only use it when you know exactly what it does.", default=None)
        parser.add_argument('--no-backup', help='do NOT backup the database. If the database is very large and you have manually backup it, using this option will fast the upgrade process. [DEFAULT] false', default=False)
        parser.add_argument('--ssh-key', help="the path of private key for SSH login $host; if provided, Ansible will use the specified key as private key to SSH login the $host", default=None)

    def run(self, args):
        yaml = '''---
- hosts: $host
  remote_user: root

  vars:
      root_password: $root_password
      login_password: $login_password
      yum_repo: "$yum_repo"

  tasks:
    - name: pre-install script
      script: $pre_install_script

    - name: set RHEL7 yum repo
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '7'
      shell: echo -e "[zstack-local]\\nname=ZStack Local Yum Repo\\nbaseurl=file://$yum_folder/static/centos7_repo\\nenabled=0\\ngpgcheck=0\\n" > /etc/yum.repos.d/zstack-local.repo

    - name: set RHEL6 yum repo
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '6' and ansible_distribution_version < '7'
      shell: echo -e "[zstack-local]\\nname=ZStack Local Yum Repo\\nbaseurl=file://$yum_folder/static/centos6_repo\\nenabled=0\\ngpgcheck=0\\n" > /etc/yum.repos.d/zstack-local.repo

    - name: install MySQL for RedHat 6 through user defined repos
      when: ansible_os_family == 'RedHat' and ansible_distribution_version < '7' and yum_repo != 'false'
      shell: yum clean metadata; yum --disablerepo=* --enablerepo={{yum_repo}} --nogpgcheck install -y mysql mysql-server 
      register: install_result

    - name: install MySQL for RedHat 6 through system defined repos
      when: ansible_os_family == 'RedHat' and ansible_distribution_version < '7' and yum_repo == 'false'
      shell: "yum clean metadata; yum --nogpgcheck install -y mysql mysql-server "
      register: install_result

    - name: install MySQL for RedHat 7 from local
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '7' and yum_repo != 'false'
      shell: yum clean metadata; yum --disablerepo=* --enablerepo={{yum_repo}} --nogpgcheck install -y  mariadb mariadb-server 
      register: install_result

    - name: install MySQL for RedHat 7 from local
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '7' and yum_repo == 'false'
      shell: yum clean metadata; yum --nogpgcheck install -y  mariadb mariadb-server
      register: install_result

    - name: install MySQL for Ubuntu
      when: ansible_os_family == 'Debian'
      apt: pkg={{item}} update_cache=yes
      with_items:
        - mysql-client
        - mysql-server
      register: install_result

    - name: open 3306 port
      shell: iptables-save | grep -- "-A INPUT -p tcp -m tcp --dport 3306 -j ACCEPT" > /dev/null || iptables -I INPUT -p tcp -m tcp --dport 3306 -j ACCEPT

    - name: run post-install script
      script: $post_install_script

    - name: enable MySQL daemon on RedHat 6
      when: ansible_os_family == 'RedHat' and ansible_distribution_version < '7'
      service: name=mysqld state=restarted enabled=yes

    - name: enable MySQL daemon on RedHat 7
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '7'
      service: name=mariadb state=restarted enabled=yes

    - name: enable MySQL on Ubuntu
      when: ansible_os_family == 'Debian'
      service: name=mysql state=restarted enabled=yes

    - name: change root password
      shell: $change_password_cmd
      register: change_root_result
      ignore_errors: yes

    - name: grant remote access
      when: change_root_result.rc == 0
      shell: $grant_access_cmd

    - name: rollback MySQL installation on RedHat 6
      when: ansible_os_family == 'RedHat' and ansible_distribution_version < '7' and change_root_result.rc != 0 and install_result.changed == True
      shell: rpm -ev mysql mysql-server

    - name: rollback MySQL installation on RedHat 7
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '7' and change_root_result.rc != 0 and install_result.changed == True
      shell: rpm -ev mariadb mariadb-server

    - name: rollback MySql installation on Ubuntu
      when: ansible_os_family == 'Debian' and change_root_result.rc != 0 and install_result.changed == True
      apt: pkg={{item}} state=absent update_cache=yes
      with_items:
        - mysql-client
        - mysql-server

    - name: failure
      fail: >
        msg="failed to change root password of MySQL, see prior error in task 'change root password'; the possible cause
        is the machine used to have MySQL installed and removed, the previous password of root user is remaining on the
        machine; try using --login-password. We have rolled back the MySQL installation so you can safely run install_db
        again with --login-password set."
      when: change_root_result.rc != 0 and install_result.changed == False
'''

        if not args.root_password and not args.login_password:
            args.root_password = '''"''"'''
            grant_access_cmd = '''/usr/bin/mysql -u root -e "GRANT ALL PRIVILEGES ON *.* TO 'root'@'localhost' IDENTIFIED BY '' WITH GRANT OPTION; GRANT ALL PRIVILEGES ON *.* TO 'root'@'%s' IDENTIFIED BY '' WITH GRANT OPTION; FLUSH PRIVILEGES;"''' % args.host
        elif not args.root_password:
            args.root_password = args.login_password
            grant_access_cmd = '''/usr/bin/mysql -u root -p%s -e "GRANT ALL PRIVILEGES ON *.* TO 'root'@'localhost' IDENTIFIED BY '%s' WITH GRANT OPTION; GRANT ALL PRIVILEGES ON *.* TO 'root'@'%s' IDENTIFIED BY '%s' WITH GRANT OPTION; FLUSH PRIVILEGES;"''' % (args.root_password, args.root_password, args.host, args.root_password)
        else:
            grant_access_cmd = '''/usr/bin/mysql -u root -p%s -e "GRANT ALL PRIVILEGES ON *.* TO 'root'@'localhost' IDENTIFIED BY '%s' WITH GRANT OPTION; GRANT ALL PRIVILEGES ON *.* TO 'root'@'%s' IDENTIFIED BY '%s' WITH GRANT OPTION; FLUSH PRIVILEGES;"''' % (args.root_password, args.root_password, args.host, args.root_password)

        if args.login_password is not None:
            change_root_password_cmd = '/usr/bin/mysqladmin -u root -p{{login_password}} password {{root_password}}'
        else:
            change_root_password_cmd = '/usr/bin/mysqladmin -u root password {{root_password}}'

        pre_install_script = '''
echo -e "#aliyun base\n[alibase]\nname=CentOS-\$releasever - Base - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$releasever/os/\$basearch/\ngpgcheck=0\nenabled=0\n \n#released updates \n[aliupdates]\nname=CentOS-\$releasever - Updates - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$releasever/updates/\$basearch/\nenabled=0\ngpgcheck=0\n \n[aliextras]\nname=CentOS-\$releasever - Extras - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$releasever/extras/\$basearch/\nenabled=0\ngpgcheck=0\n \n[aliepel]\nname=Extra Packages for Enterprise Linux \$releasever - \$basearce - mirrors.aliyun.com\nbaseurl=http://mirrors.aliyun.com/epel/\$releasever/\$basearch\nfailovermethod=priority\nenabled=0\ngpgcheck=0\n" > /etc/yum.repos.d/zstack-aliyun-yum.repo

echo -e "#163 base\n[163base]\nname=CentOS-\$releasever - Base - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$releasever/os/\$basearch/\ngpgcheck=0\nenabled=0\n \n#released updates \n[163updates]\nname=CentOS-\$releasever - Updates - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$releasever/updates/\$basearch/\nenabled=0\ngpgcheck=0\n \n#additional packages that may be useful\n[163extras]\nname=CentOS-\$releasever - Extras - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$releasever/extras/\$basearch/\nenabled=0\ngpgcheck=0\n \n[ustcepel]\nname=Extra Packages for Enterprise Linux \$releasever - \$basearch - ustc \nbaseurl=http://centos.ustc.edu.cn/epel/\$releasever/\$basearch\nfailovermethod=priority\nenabled=0\ngpgcheck=0\n" > /etc/yum.repos.d/zstack-163-yum.repo

###################
#Check DNS hijacking
###################

hostname=`hostname`

pintret=`ping -c 1 -W 2 $hostname 2>/dev/null | head -n1`
echo $pintret | grep 'PING' > /dev/null
[ $? -ne 0 ] && exit 0

ip=`echo $pintret | cut -d' ' -f 3 | cut -d'(' -f 2 | cut -d')' -f 1`

ip_1=`echo $ip | cut -d'.' -f 1`
[ "127" = "$ip_1" ] && exit 0

ip addr | grep $ip > /dev/null
[ $? -eq 0 ] && exit 0

echo "The hostname($hostname) of your machine is resolved to IP($ip) which is none of IPs of your machine.
It's likely your DNS server has been hijacking, please try fixing it or add \"ip_of_your_host $hostname\" to /etc/hosts.
DNS hijacking will cause MySQL and RabbitMQ not working."
exit 1
'''
        fd, pre_install_script_path = tempfile.mkstemp()
        os.fdopen(fd, 'w').write(pre_install_script)

        def cleanup_pre_install_script():
            os.remove(pre_install_script_path)

        self.install_cleanup_routine(cleanup_pre_install_script)

        post_install_script = '''
if [ -f /etc/mysql/my.cnf ]; then
    # Ubuntu
    sed -i 's/^bind-address/#bind-address/' /etc/mysql/my.cnf
    sed -i 's/^skip-networking/#skip-networking/' /etc/mysql/my.cnf
    grep '^character-set-server' /etc/mysql/my.cnf >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        sed -i '/\[mysqld\]/a character-set-server=utf8\' /etc/mysql/my.cnf
    fi
    grep '^skip-name-resolve' /etc/mysql/my.cnf >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        sed -i '/\[mysqld\]/a skip-name-resolve\' /etc/mysql/my.cnf
    fi
fi

if [ -f /etc/my.cnf ]; then
    # CentOS
    sed -i 's/^bind-address/#bind-address/' /etc/my.cnf
    sed -i 's/^skip-networking/#skip-networking/' /etc/my.cnf
    grep '^character-set-server' /etc/my.cnf >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        sed -i '/\[mysqld\]/a character-set-server=utf8\' /etc/my.cnf
    fi
    grep '^skip-name-resolve' /etc/my.cnf >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        sed -i '/\[mysqld\]/a skip-name-resolve\' /etc/my.cnf
    fi
fi
'''
        fd, post_install_script_path = tempfile.mkstemp()
        os.fdopen(fd, 'w').write(post_install_script)

        def cleanup_post_install_script():
            os.remove(post_install_script_path)

        self.install_cleanup_routine(cleanup_post_install_script)

        t = string.Template(yaml)
        if args.yum:
            yum_repo = args.yum
        else:
            yum_repo = 'false'
        yaml = t.substitute({
            'host': args.host,
            'change_password_cmd': change_root_password_cmd,
            'root_password': args.root_password,
            'login_password': args.login_password,
            'grant_access_cmd': grant_access_cmd,
            'pre_install_script': pre_install_script_path,
            'yum_folder': ctl.zstack_home,
            'yum_repo': yum_repo,
            'post_install_script': post_install_script_path
        })

        ansible(yaml, args.host, args.debug, args.ssh_key)

class InstallRabbitCmd(Command):
    def __init__(self):
        super(InstallRabbitCmd, self).__init__()
        self.name = "install_rabbitmq"
        self.description = "install RabbitMQ message broker on local or remote machine."
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help='host IP, for example, 192.168.0.212, please specify the real IP rather than "localhost" or "127.0.0.1" when installing on local machine; otherwise management nodes on other machines cannot access the RabbitMQ.', required=True)
        parser.add_argument('--debug', help="open Ansible debug option", action="store_true", default=False)
        parser.add_argument('--no-update', help="don't update the IP address to 'CloudBus.serverIp.0' in zstack.properties", action="store_true", default=False)
        parser.add_argument('--ssh-key', help="the path of private key for SSH login $host; if provided, Ansible will use the specified key as private key to SSH login the $host", default=None)
        parser.add_argument('--rabbit-username', help="RabbitMQ username; if set, the username will be created on RabbitMQ. [DEFAULT] rabbitmq default username", default=None)
        parser.add_argument('--rabbit-password', help="RabbitMQ password; if set, the password will be created on RabbitMQ for username specified by --rabbit-username. [DEFAULT] rabbitmq default password", default=None)
        parser.add_argument('--yum', help="Use ZStack predefined yum repositories. The valid options include: alibase,aliepel,163base,ustcepel,zstack-local. NOTE: only use it when you know exactly what it does.", default=None)

    def run(self, args):
        if (args.rabbit_password is None and args.rabbit_username) or (args.rabbit_username and args.rabbit_password is None):
            raise CtlError('--rabbit-username and --rabbit-password must be both set or not set')

        yaml = '''---
- hosts: $host
  remote_user: root

  vars:
      yum_repo: "$yum_repo"

  tasks:
    - name: pre-install script
      script: $pre_install_script

    - name: set RHEL7 yum repo
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '7'
      shell: echo -e "[zstack-local]\\nname=ZStack Local Yum Repo\\nbaseurl=file://$yum_folder/static/centos7_repo\\nenabled=0\\ngpgcheck=0\\n" > /etc/yum.repos.d/zstack-local.repo

    - name: set RHEL6 yum repo
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '6' and ansible_distribution_version < '7'
      shell: echo -e "[zstack-local]\\nname=ZStack Local Yum Repo\\nbaseurl=file://$yum_folder/static/centos6_repo\\nenabled=0\\ngpgcheck=0\\n" > /etc/yum.repos.d/zstack-local.repo

    - name: install RabbitMQ on RedHat OS from user defined yum repo
      when: ansible_os_family == 'RedHat' and yum_repo != 'false'
      shell: yum clean metadata; yum --disablerepo=* --enablerepo={{yum_repo}} --nogpgcheck install -y rabbitmq-server libselinux-python 

    - name: install RabbitMQ on RedHat OS from online
      when: ansible_os_family == 'RedHat' and yum_repo == 'false'
      shell: yum clean metadata; yum --nogpgcheck install -y rabbitmq-server libselinux-python

    - name: install RabbitMQ on Ubuntu OS
      when: ansible_os_family == 'Debian'
      apt: pkg={{item}} update_cache=yes
      with_items:
        - rabbitmq-server


    - name: open 5672 port
      shell: iptables-save | grep -- "-A INPUT -p tcp -m tcp --dport 5672 -j ACCEPT" > /dev/null || iptables -I INPUT -p tcp -m tcp --dport 5672 -j ACCEPT

    - name: open 5673 port
      shell: iptables-save | grep -- "-A INPUT -p tcp -m tcp --dport 5673 -j ACCEPT" > /dev/null || iptables -I INPUT -p tcp -m tcp --dport 5673 -j ACCEPT

    - name: open 15672 port
      shell: iptables-save | grep -- "-A INPUT -p tcp -m tcp --dport 15672 -j ACCEPT" > /dev/null || iptables -I INPUT -p tcp -m tcp --dport 15672 -j ACCEPT

    - name: enable RabbitMQ
      service: name=rabbitmq-server state=started enabled=yes

    - name: post-install script
      script: $post_install_script
'''

        pre_script = '''
echo -e "#aliyun base\n[alibase]\nname=CentOS-\$releasever - Base - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$releasever/os/\$basearch/\ngpgcheck=0\nenabled=0\n \n#released updates \n[aliupdates]\nname=CentOS-\$releasever - Updates - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$releasever/updates/\$basearch/\nenabled=0\ngpgcheck=0\n \n[aliextras]\nname=CentOS-\$releasever - Extras - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$releasever/extras/\$basearch/\nenabled=0\ngpgcheck=0\n \n[aliepel]\nname=Extra Packages for Enterprise Linux \$releasever - \$basearce - mirrors.aliyun.com\nbaseurl=http://mirrors.aliyun.com/epel/\$releasever/\$basearch\nfailovermethod=priority\nenabled=0\ngpgcheck=0\n" > /etc/yum.repos.d/zstack-aliyun-yum.repo

echo -e "#163 base\n[163base]\nname=CentOS-\$releasever - Base - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$releasever/os/\$basearch/\ngpgcheck=0\nenabled=0\n \n#released updates \n[163updates]\nname=CentOS-\$releasever - Updates - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$releasever/updates/\$basearch/\nenabled=0\ngpgcheck=0\n \n#additional packages that may be useful\n[163extras]\nname=CentOS-\$releasever - Extras - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$releasever/extras/\$basearch/\nenabled=0\ngpgcheck=0\n \n[ustcepel]\nname=Extra Packages for Enterprise Linux \$releasever - \$basearch - ustc \nbaseurl=http://centos.ustc.edu.cn/epel/\$releasever/\$basearch\nfailovermethod=priority\nenabled=0\ngpgcheck=0\n" > /etc/yum.repos.d/zstack-163-yum.repo

###################
#Check DNS hijacking
###################

hostname=`hostname`

pintret=`ping -c 1 -W 2 $hostname 2>/dev/null | head -n1`
echo $pintret | grep 'PING' > /dev/null
[ $? -ne 0 ] && exit 0

ip=`echo $pintret | cut -d' ' -f 3 | cut -d'(' -f 2 | cut -d')' -f 1`

ip_1=`echo $ip | cut -d'.' -f 1`
[ "127" = "$ip_1" ] && exit 0

ip addr | grep $ip > /dev/null
[ $? -eq 0 ] && exit 0

echo "The hostname($hostname) of your machine is resolved to IP($ip) which is none of IPs of your machine.
It's likely your DNS server has been hijacking, please try fixing it or add \"ip_of_your_host $hostname\" to /etc/hosts.
DNS hijacking will cause MySQL and RabbitMQ not working."
exit 1
'''
        fd, pre_script_path = tempfile.mkstemp()
        os.fdopen(fd, 'w').write(pre_script)

        def cleanup_prescript():
            os.remove(pre_script_path)

        self.install_cleanup_routine(cleanup_prescript)

        if args.rabbit_username and args.rabbit_password:
            post_script = '''set -e
rabbitmqctl add_user $username $password
rabbitmqctl set_user_tags $username administrator
rabbitmqctl set_permissions -p / $username ".*" ".*" ".*"
'''
            t = string.Template(post_script)
            post_script = t.substitute({
                'username': args.rabbit_username,
                'password': args.rabbit_password
            })
        else:
            post_script = ''

        fd, post_script_path = tempfile.mkstemp()
        os.fdopen(fd, 'w').write(post_script)

        def cleanup_postscript():
            os.remove(post_script_path)

        self.install_cleanup_routine(cleanup_postscript)

        t = string.Template(yaml)
        if args.yum:
            yum_repo = args.yum
        else:
            yum_repo = 'false'
        yaml = t.substitute({
            'host': args.host,
            'pre_install_script': pre_script_path,
            'yum_folder': ctl.zstack_home,
            'yum_repo': yum_repo,
            'post_install_script': post_script_path
        })

        ansible(yaml, args.host, args.debug, args.ssh_key)

        if not args.no_update:
            ctl.write_property('CloudBus.serverIp.0', args.host)
            info('updated CloudBus.serverIp.0=%s in %s' % (args.host, ctl.properties_file_path))

        if args.rabbit_username and args.rabbit_password:
            ctl.write_property('CloudBus.rabbitmqUsername', args.rabbit_username)
            info('updated CloudBus.rabbitmqUsername=%s in %s' % (args.rabbit_username, ctl.properties_file_path))
            ctl.write_property('CloudBus.rabbitmqPassword', args.rabbit_password)
            info('updated CloudBus.rabbitmqPassword=%s in %s' % (args.rabbit_password, ctl.properties_file_path))

class InstallKairosdbCmd(Command):
    PACKAGE_NAME = "kairosdb-1.1.1-1.tar.gz"
    KAIROSDB_EXEC = 'KAIROSDB_EXEC'
    KAIROSDB_CONF = 'KAIROSDB_CONF'
    KAIROSDB_LOG = 'KAIROSDB_LOG'

    def __init__(self):
        super(InstallKairosdbCmd, self).__init__()
        self.name = "install_kairosdb"
        self.description = (
            "install kairosdb"
        )
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--file', help='path to the %s' % self.PACKAGE_NAME, required=False)
        parser.add_argument('--listen-address', help='the IP kairosdb listens to, which cannot be 0.0.0.0', required=True)
        parser.add_argument('--cassandra-rpc-address', help='the RPC address of cassandra, which must be in the format of'
                                                            '\nIP:port, for example, 192.168.0.199:9160. If omitted, the'
                                                            '\naddress will be retrieved from local cassandra YAML config,'
                                                            '\nor an error will be raised if the YAML config cannot be found', required=False)
        parser.add_argument('--listen-port', help='the port kairosdb listens to, default to 18080', default=18080, required=False)
        parser.add_argument('--update-zstack-config', action='store_true', default=True, help='update kairosdb config to zstack.properties', required=False)

    def run(self, args):
        if not args.file:
            args.file = os.path.join(ctl.USER_ZSTACK_HOME_DIR, self.PACKAGE_NAME)

        if not os.path.exists(args.file):
            raise CtlError('cannot find %s, you may need to specify the option[--file]' % args.file)

        if not args.file.endswith(self.PACKAGE_NAME):
            raise CtlError('at this version, zstack only supports %s' % self.PACKAGE_NAME)

        shell('su - zstack -c "tar xzf %s -C %s"' % (args.file, ctl.USER_ZSTACK_HOME_DIR))
        kairosdb_dir = os.path.join(ctl.USER_ZSTACK_HOME_DIR, "kairosdb")
        info("successfully installed %s to %s" % (args.file, os.path.join(ctl.USER_ZSTACK_HOME_DIR, kairosdb_dir)))

        if args.listen_address == '0.0.0.0':
            raise CtlError('for your data safety, please do NOT use 0.0.0.0 as the listen address')

        original_conf_path = os.path.join(kairosdb_dir, "conf/kairosdb.properties")
        shell("yes | cp %s %s.bak" % (original_conf_path, original_conf_path))

        all_configs = []
        if ctl.extra_arguments:
            configs = [l.split('=', 1) for l in ctl.extra_arguments]
            for l in configs:
                if len(l) != 2:
                    raise CtlError('invalid config[%s]. The config must be in the format of key=value without spaces around the =' % l)
                all_configs.append(l)

        if args.cassandra_rpc_address and ':' not in args.cassandra_rpc_address:
            raise CtlError('invalid --cassandra-rpc-address[%s]. It must be in the format of IP:port' % args.cassandra_rpc_address)
        elif not args.cassandra_rpc_address:
            cassandra_conf = ctl.get_env(InstallCassandraCmd.CASSANDRA_CONF)
            if not cassandra_conf:
                raise CtlError('cannot find cassandra conf[%s] in %s, have you installed cassandra? or'
                               ' you can use --cassandra-rpc-address to set the address explicitly' % (InstallCassandraCmd.CASSANDRA_CONF, SetEnvironmentVariableCmd.PATH))

            with open(cassandra_conf, 'r') as fd:
                with on_error('cannot YAML load %s, it seems corrupted' % InstallCassandraCmd.CASSANDRA_CONF):
                    c_conf = yaml.load(fd.read())

            addr = c_conf['rpc_address']
            if not addr:
                raise CtlError('rpc_address is not set in %s. Please fix it otherwise kairosdb cannot boot later' % InstallCassandraCmd.CASSANDRA_CONF)

            port = c_conf['rpc_port']
            if not port:
                raise CtlError('rpc_port is not set in %s. Please fix it otherwise kairosdb cannot boot later' % InstallCassandraCmd.CASSANDRA_CONF)

            args.cassandra_rpc_address = '%s:%s' % (addr, port)

        all_configs.extend([
          ('kairosdb.service.datastore', 'org.kairosdb.datastore.cassandra.CassandraModule'),
          ('kairosdb.jetty.address', args.listen_address),
          ('kairosdb.jetty.port', args.listen_port),
          ('kairosdb.datastore.cassandra.host_list', args.cassandra_rpc_address)
        ])
        prop = PropertyFile(original_conf_path)
        prop.write_properties(all_configs)

        if args.update_zstack_config:
            ctl.write_properties([
              ('Kairosdb.ip', args.listen_address),
              ('Kairosdb.port', args.listen_port),
            ])
            info('successfully wrote kairosdb properties to %s' % ctl.properties_file_path)

        ctl.put_envs([
            (self.KAIROSDB_EXEC, os.path.normpath('%s/bin/kairosdb.sh' % kairosdb_dir)),
            (self.KAIROSDB_CONF, original_conf_path),
            (self.KAIROSDB_LOG, os.path.join(kairosdb_dir, 'log')),
        ])

        log_conf = os.path.normpath('%s/conf/logging/logback.xml' % kairosdb_dir)
        shell('''sed -i 's/<root level="DEBUG">/<root level="INFO">/g' %s''' % log_conf)

        info('successfully installed kairosdb, the config file is written to %s' % original_conf_path)

class InstallCassandraCmd(Command):
    CASSANDRA_EXEC = 'CASSANDRA_EXEC'
    CASSANDRA_CONF = 'CASSANDRA_CONF'
    CASSANDRA_LOG = 'CASSANDRA_LOG'

    def __init__(self):
        super(InstallCassandraCmd, self).__init__()
        self.name = "install_cassandra"
        self.description = (
            "install cassandra nosql database."
            "\nNOTE: you can pass an extra JSON string that will be converted to the cassandra YAML config. The string must"
            "\nbe quoted by a single quote('), and the content must be the valid JSON format, for example:"
            "\n\nzstack-ctl  install_cassandra --file /tmp/apache-cassandra-2.2.3-bin.tar.gz '{\"rpc_address\":\"192.168.0.199\", \"listen_address\":\"192.168.0.199\"}'"
        )
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--file', help='path to the apache-cassandra-2.2.3-bin.tar.gz', required=False)
        parser.add_argument('--user-zstack', help='do all operations with user zstack', default=True, action='store_true', required=False)
        parser.add_argument('--listen-address', help='the IP used for both rpc_address and listen_address.'
                                                     'This option is overridden if rpc_address or listen_address'
                                                     ' is specified in the JSON body', required=False)

    def run(self, args):
        if not args.file:
            args.file = os.path.join(ctl.USER_ZSTACK_HOME_DIR, "apache-cassandra-2.2.3-bin.tar.gz")

        if not os.path.exists(args.file):
            raise CtlError('cannot find %s, you may need to specify the option[--file]' % args.file)

        if not args.file.endswith("apache-cassandra-2.2.3-bin.tar.gz"):
            raise CtlError('at this version, zstack only support apache-cassandra-2.2.3-bin.tar.gz')

        shell('su - zstack -c "tar xzf %s -C %s"' % (args.file, ctl.USER_ZSTACK_HOME_DIR))
        cassandra_dir = os.path.join(ctl.USER_ZSTACK_HOME_DIR, "apache-cassandra-2.2.3")
        info("successfully installed %s to %s" % (args.file, os.path.join(ctl.USER_ZSTACK_HOME_DIR, cassandra_dir)))

        yaml_conf = os.path.join(cassandra_dir, "conf/cassandra.yaml")
        shell('yes | cp %s %s.bak' % (yaml_conf, yaml_conf))

        if ctl.extra_arguments:
            extra = ' '.join(ctl.extra_arguments)
            with on_error("%s is not a valid JSON string" % extra):
                conf = simplejson.loads(extra)
        else:
            conf = {}

        if args.listen_address:
            if 'rpc_address' not in conf:
                conf['rpc_address'] = args.listen_address
            if 'listen_address' not in conf:
                conf['listen_address'] = args.listen_address

        if 'commitlog_directory' not in conf:
            conf['commitlog_directory'] = ['/var/lib/cassandra/commitlog']
        if 'data_file_directories' not in conf:
            conf['data_file_directories'] = ['/var/lib/cassandra/data']
        if 'commitlog_directory' not in conf:
            conf['saved_caches_directory'] = ['/var/lib/cassandra/saved_caches']
        conf['start_rpc'] = True

        if args.user_zstack:
            with use_user_zstack():
                with open(yaml_conf, 'r') as fd:
                    c_conf = yaml.load(fd.read())
        else:
            with open(yaml_conf, 'r') as fd:
                c_conf = yaml.load(fd.read())

        for k, v in conf.items():
            c_conf[k] = v

        listen_address = c_conf['listen_address']
        rpc_address = c_conf['rpc_address']
        if listen_address != rpc_address:
            raise CtlError('listen_address[%s] and rpc_address[%s] do not match' % (listen_address, rpc_address))

        seed_provider = c_conf['seed_provider']
        with on_error("cannot find parameter[seeds] in %s" % yaml_conf):
            # check if the parameter is in the YAML conf
            _ = seed_provider[0]['parameters'][0]['seeds']

        seed_provider[0]['parameters'][0]['seeds'] = listen_address
        info("change parameter['seeds'] to listen_address[%s], otherwise cassandra may fail to get seeds" % listen_address)

        if args.user_zstack:
            with use_user_zstack():
                with open(yaml_conf, 'w') as fd:
                    fd.write(yaml.dump(c_conf, default_flow_style=False))
        else:
            with open(yaml_conf, 'w') as fd:
                fd.write(yaml.dump(c_conf, default_flow_style=False))

        ctl.put_envs([
          (self.CASSANDRA_EXEC, os.path.join(cassandra_dir, 'bin/cassandra')),
          (self.CASSANDRA_CONF, yaml_conf),
          (self.CASSANDRA_LOG, os.path.join(cassandra_dir, 'log')),
        ])
        info('configs are written into %s' % yaml_conf)

class KairosdbCmd(Command):
    NAME = 'kairosdb'

    def __init__(self):
        super(KairosdbCmd, self).__init__()
        self.name = self.NAME
        self.description = (
            'control kairosdb life cycle'
        )
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--start', help='start kairosdb', action="store_true", required=False)
        parser.add_argument('--stop', help='stop kairosdb', action="store_true", required=False)
        parser.add_argument('--status', help='show kairosdb status', action="store_true", required=False)
        parser.add_argument('--wait-timeout', type=int, help='wait timeout(in seconds) until kairosdb web port is available. This is normally used'
                                                           ' with --start option to make sure cassandra successfully starts.',
                            default=-1, required=False)

    def _status(self, args):
        return find_process_by_cmdline('org.kairosdb.core.Main')

    def start(self, args):
        pid = self._status(args)
        if pid:
            info('kairosdb[PID:%s] is already running' % pid)
            return

        exe = ctl.get_env(InstallKairosdbCmd.KAIROSDB_EXEC)
        if not os.path.exists(exe):
            raise CtlError('cannot find the variable[%s] in %s. Have you installed kaiosdb?' %
                           (InstallKairosdbCmd.KAIROSDB_EXEC, SetEnvironmentVariableCmd.PATH))

        shell('bash %s start' % exe)
        info('successfully starts kairosdb')

        if args.wait_timeout < 0:
            return

        info('waiting for kairosdb to listen on web port until %s seconds timeout' % args.wait_timeout)
        conf = ctl.get_env(InstallKairosdbCmd.KAIROSDB_CONF)
        if not conf:
            warn('cannot find the variable[%s] in %s, ignore --wait-timeout' %
                (InstallKairosdbCmd.KAIROSDB_CONF, SetEnvironmentVariableCmd.PATH))
            return

        if not os.path.exists(conf):
            warn('cannot find kairosdb conf at %s, ignore --wait-timeout' % conf)
            return

        prop = PropertyFile(conf)
        port = prop.read_property('kairosdb.jetty.port')
        if not port:
            raise CtlError('kairosdb.jetty.port is not set in %s' % InstallKairosdbCmd.KAIROSDB_CONF)

        while args.wait_timeout > 0:
            ret = shell_return('netstat -nap | grep %s > /dev/null' % port)
            if ret == 0:
                info('kairosdb is listening on the web port[%s] now' % port)
                return
            time.sleep(1)
            args.wait_timeout -= 1

        raise CtlError("kairosdb is not listening on the web port[%s] after %s seconds, it may not successfully start,"
                        "please check the log file in %s" % (port, args.wait_timeout, ctl.get_env(InstallKairosdbCmd.KAIROSDB_CONF)))

    def stop(self, args):
        pid = self._status(args)
        if not pid:
            info('kairosdb is already stopped')
            return

        exe = ctl.get_env(InstallKairosdbCmd.KAIROSDB_EXEC)
        if not os.path.exists(exe):
            shell('kill %s' % pid)
        else:
            shell('bash %s stop' % exe)

        count = 30
        while count > 0:
            pid = self._status(args)
            if not pid:
                info('successfully stopped kairosdb')
                return
            time.sleep(1)
            count -= 1

        info('kairosdb is still running after %s seconds, kill -9 it' % count)
        shell('kill -9 %s' % pid)

    def status(self, args):
        pid = self._status(args)
        if pid:
            info('kairosdb[PID:%s] is running' % pid)
        else:
            info('kairosdb is stopped')

    def run(self, args):
        if args.start:
            self.start(args)
        elif args.stop:
            self.stop(args)
        elif args.status:
            self.status(args)
        else:
            self.status(args)

class CassandraCmd(Command):
    def __init__(self):
        super(CassandraCmd, self).__init__()
        self.name = "cassandra"
        self.description = (
            "control cassandra's life cycle"
        )
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--start', help='start cassandra', action="store_true", required=False)
        parser.add_argument('--stop', help='stop cassandra', action="store_true", required=False)
        parser.add_argument('--status', help='show cassandra status', action="store_true", required=False)
        parser.add_argument('--wait-timeout', type=int, help='wait timeout(in seconds) until cassandra RPC port is available. This is normally used'
                                           ' with --start option to make sure cassandra successfully starts.',
                            default=-1, required=False)

    def start(self, args):
        pid = self._status(args)
        if pid:
            info('cassandra[PID:%s] is already running' % pid)
            return

        exe = ctl.get_env(InstallCassandraCmd.CASSANDRA_EXEC)
        if not exe:
            raise CtlError('cannot find the variable[%s] in %s. Have you installed cassandra?' %
                           (InstallCassandraCmd.CASSANDRA_EXEC, SetEnvironmentVariableCmd.PATH))

        # cd to the /bin folder to start cassandra, otherwise the command line
        # will be too long to be truncated by the linux /proc/[pid]/cmdline, which
        # leads _status() not working
        shell('cd %s && bash %s' % (os.path.dirname(exe), os.path.basename(exe)))
        info('successfully starts cassandra')

        if args.wait_timeout <= 0:
            return

        info('waiting for cassandra to listen on the RPC port until timeout after %s seconds' % args.wait_timeout)
        conf = ctl.get_env(InstallCassandraCmd.CASSANDRA_CONF)
        if not conf:
            warn('cannot find the variable[%s] in %s, ignore --wait-timeout' %
                (InstallCassandraCmd.CASSANDRA_CONF, SetEnvironmentVariableCmd.PATH))
            return

        if not os.path.exists(conf):
            warn('cannot find cassandra conf at %s, ignore --wait-timeout' % conf)
            return

        with open(conf, 'r') as fd:
            m = yaml.load(fd.read())
            port = m['rpc_port']
            if not port:
                warn('cannot find parameter[rpc_port] in %s, ignore --wait-timeout' % conf)

            while args.wait_timeout > 0:
                ret = shell_return('netstat -nap | grep %s > /dev/null' % port)
                if ret == 0:
                    info('cassandra is listening on RPC port[%s] now' % port)
                    return

                time.sleep(1)
                args.wait_timeout -= 1

            raise CtlError("cassandra is not listening on RPC port[%s] after %s seconds, it may not successfully start,"
                           "please check the log file in %s" % (port, args.wait_timeout, ctl.get_env(InstallCassandraCmd.CASSANDRA_LOG)))

    def stop(self, args):
        pid = self._status(args)
        if not pid:
            info('cassandra is already stopped')
            return

        shell('kill %s' % pid)

        count = 30
        while count > 0:
            pid = self._status(args)
            if not pid:
                info('successfully stopped cassandra')
                return
            time.sleep(1)
            count -= 1

        info('cassandra is still running after %s seconds, kill -9 it' % count)
        shell('kill -9 %s' % pid)

    def _status(self, args):
        return find_process_by_cmdline('org.apache.cassandra.service.CassandraDaemon')

    def status(self, args):
        pid = self._status(args)
        if not pid:
            info('cassandra is stopped')
        else:
            info('cassandra[PID:%s] is running' % pid)

    def run(self, args):
        if args.start:
            self.start(args)
        elif args.stop:
            self.stop(args)
        elif args.status:
            self.status(args)
        else:
            self.status(args)

class InstallManagementNodeCmd(Command):
    def __init__(self):
        super(InstallManagementNodeCmd, self).__init__()
        self.name = "install_management_node"
        self.description = (
            "install ZStack management node from current machine to a remote machine with zstack.properties."
            "\nNOTE: please configure current node before installing node on other machines"
        )
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help='target host IP, for example, 192.168.0.212, to install ZStack management node to a remote machine', required=True)
        parser.add_argument('--install-path', help='the path on remote machine where Apache Tomcat will be installed, which must be an absolute path; [DEFAULT]: /usr/local/zstack', default='/usr/local/zstack')
        parser.add_argument('--source-dir', help='the source folder containing Apache Tomcat package and zstack.war, if omitted, it will default to a path related to $ZSTACK_HOME')
        parser.add_argument('--debug', help="open Ansible debug option", action="store_true", default=False)
        parser.add_argument('--force-reinstall', help="delete existing Apache Tomcat and resinstall ZStack", action="store_true", default=False)
        parser.add_argument('--yum', help="Use ZStack predefined yum repositories. The valid options include: alibase,aliepel,163base,ustcepel,zstack-local. NOTE: only use it when you know exactly what it does.", default=None)
        parser.add_argument('--ssh-key', help="the path of private key for SSH login $host; if provided, Ansible will use the specified key as private key to SSH login the $host", default=None)

    def run(self, args):
        if not os.path.isabs(args.install_path):
            raise CtlError('%s is not an absolute path' % args.install_path)

        if not args.source_dir:
            args.source_dir = os.path.join(ctl.zstack_home, "../../../")

        if not os.path.isdir(args.source_dir):
            raise CtlError('%s is not an directory' % args.source_dir)

        apache_tomcat = None
        zstack = None
        apache_tomcat_zip_name = None
        for file in os.listdir(args.source_dir):
            full_path = os.path.join(args.source_dir, file)
            if file.startswith('apache-tomcat') and file.endswith('zip') and os.path.isfile(full_path):
                apache_tomcat = full_path
                apache_tomcat_zip_name = file
            if file == 'zstack.war':
                zstack = full_path

        if not apache_tomcat:
            raise CtlError('cannot find Apache Tomcat ZIP in %s, please use --source-dir to specify the directory containing the ZIP' % args.source_dir)

        if not zstack:
            raise CtlError('cannot find zstack.war in %s, please use --source-dir to specify the directory containing the WAR file' % args.source_dir)

        pypi_path = os.path.join(ctl.zstack_home, "static/pypi/")
        if not os.path.isdir(pypi_path):
            raise CtlError('cannot find %s, please make sure you have installed ZStack management node' % pypi_path)

        pypi_tar_path = os.path.join(ctl.zstack_home, "static/pypi.tar.bz")
        static_path = os.path.join(ctl.zstack_home, "static")
        shell('cd %s; tar jcf pypi.tar.bz pypi' % static_path)

        yaml = '''---
- hosts: $host
  remote_user: root

  vars:
      root: $install_path
      yum_repo: "$yum_repo"

  tasks:
    - name: check remote env on RedHat OS 6
      when: ansible_os_family == 'RedHat' and ansible_distribution_version < '7'
      script: $pre_script_on_rh6

    - name: prepare remote environment
      script: $pre_script

    - name: set RHEL7 yum repo
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '7'
      shell: echo -e "[zstack-local]\\nname=ZStack Local Yum Repo\\nbaseurl=file://$yum_folder/static/centos7_repo\\nenabled=0\\ngpgcheck=0\\n" > /etc/yum.repos.d/zstack-local.repo

    - name: set RHEL6 yum repo
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '6' and ansible_distribution_version < '7'
      shell: echo -e "[zstack-local]\\nname=ZStack Local Yum Repo\\nbaseurl=file://$yum_folder/static/centos6_repo\\nenabled=0\\ngpgcheck=0\\n" > /etc/yum.repos.d/zstack-local.repo

    - name: install dependencies on RedHat OS from user defined repo
      when: ansible_os_family == 'RedHat' and yum_repo != 'false'
      shell: yum clean metadata; yum --disablerepo=* --enablerepo={{yum_repo}} --nogpgcheck install -y java-1.7.0-openjdk wget python-devel gcc autoconf tar gzip unzip python-pip openssh-clients sshpass bzip2 ntp ntpdate sudo libselinux-python 

    - name: install dependencies on RedHat OS from system repos
      when: ansible_os_family == 'RedHat' and yum_repo == 'false'
      shell: yum clean metadata; yum --nogpgcheck install -y java-1.7.0-openjdk wget python-devel gcc autoconf tar gzip unzip python-pip openssh-clients sshpass bzip2 ntp ntpdate sudo libselinux-python

    - name: install dependencies Debian OS
      when: ansible_os_family == 'Debian'
      apt: pkg={{item}} update_cache=yes
      with_items:
        - openjdk-7-jdk
        - wget
        - python-dev
        - gcc
        - autoconf
        - tar
        - gzip
        - unzip
        - python-pip
        - sshpass
        - bzip2
        - ntp
        - ntpdate
        - sudo

    - name: install MySQL client for RedHat 6 from user defined repos
      when: ansible_os_family == 'RedHat' and ansible_distribution_version < '7' and yum_repo != 'false'
      shell: yum --disablerepo=* --enablerepo={{yum_repo}} --nogpgcheck install -y mysql

    - name: install MySQL client for RedHat 6 from system repo
      when: ansible_os_family == 'RedHat' and ansible_distribution_version < '7' and yum_repo == 'false'
      shell: yum --nogpgcheck install -y mysql

    - name: install MySQL client for RedHat 7 from user defined repos
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '7' and yum_repo != 'false'
      shell: yum --disablerepo=* --enablerepo={{yum_repo}} --nogpgcheck install -y mariadb 

    - name: install MySQL client for RedHat 7 from system repos
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '7' and yum_repo == 'false'
      shell: yum --nogpgcheck install -y mariadb

    - name: install MySQL client for Ubuntu
      when: ansible_os_family == 'Debian'
      apt: pkg={{item}}
      with_items:
        - mysql-client

    - name: copy pypi tar file
      copy: src=$pypi_tar_path dest=$pypi_tar_path_dest

    - name: untar pypi
      shell: "cd /tmp/; tar jxf $pypi_tar_path_dest"

    - name: install pip from local source
      shell: "cd $pypi_path; pip install --ignore-installed pip*.tar.gz"

    - name: install virtualenv
      pip: name="virtualenv" extra_args="-i file://$pypi_path/simple --ignore-installed --trusted-host localhost"

    - name: copy Apache Tomcat
      copy: src=$apache_path dest={{root}}/$apache_tomcat_zip_name

    - name: copy zstack.war
      copy: src=$zstack_path dest={{root}}/zstack.war

    - name: install ZStack
      script: $post_script

    - name: copy zstack.properties
      copy: src=$properties_file dest={{root}}/apache-tomcat/webapps/zstack/WEB-INF/classes/zstack.properties

    - name: setup zstack account
      script: $setup_account
'''

        pre_script = '''
echo -e "#aliyun base\n[alibase]\nname=CentOS-\$$releasever - Base - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$$releasever/os/\$$basearch/\ngpgcheck=0\nenabled=0\n \n#released updates \n[aliupdates]\nname=CentOS-\$$releasever - Updates - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$$releasever/updates/\$$basearch/\nenabled=0\ngpgcheck=0\n \n[aliextras]\nname=CentOS-\$$releasever - Extras - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$$releasever/extras/\$$basearch/\nenabled=0\ngpgcheck=0\n \n[aliepel]\nname=Extra Packages for Enterprise Linux \$$releasever - \$$basearce - mirrors.aliyun.com\nbaseurl=http://mirrors.aliyun.com/epel/\$$releasever/\$$basearch\nfailovermethod=priority\nenabled=0\ngpgcheck=0\n" > /etc/yum.repos.d/zstack-aliyun-yum.repo

echo -e "#163 base\n[163base]\nname=CentOS-\$$releasever - Base - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$$releasever/os/\$$basearch/\ngpgcheck=0\nenabled=0\n \n#released updates \n[163updates]\nname=CentOS-\$$releasever - Updates - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$$releasever/updates/\$$basearch/\nenabled=0\ngpgcheck=0\n \n#additional packages that may be useful\n[163extras]\nname=CentOS-\$$releasever - Extras - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$$releasever/extras/\$$basearch/\nenabled=0\ngpgcheck=0\n \n[ustcepel]\nname=Extra Packages for Enterprise Linux \$$releasever - \$$basearch - ustc \nbaseurl=http://centos.ustc.edu.cn/epel/\$$releasever/\$$basearch\nfailovermethod=priority\nenabled=0\ngpgcheck=0\n" > /etc/yum.repos.d/zstack-163-yum.repo

whereis zstack-ctl
if [ $$? -eq 0 ]; then
   zstack-ctl stop_node
fi

apache_path=$install_path/apache-tomcat
if [[ -d $$apache_path ]] && [[ $force_resinstall -eq 0 ]]; then
   echo "found existing Apache Tomcat directory $$apache_path; please use --force-reinstall to delete it and re-install"
   exit 1
fi

rm -rf $install_path
mkdir -p $install_path
'''
        t = string.Template(pre_script)
        pre_script = t.substitute({
            'force_resinstall': int(args.force_reinstall),
            'install_path': args.install_path
        })

        fd, pre_script_path = tempfile.mkstemp(suffix='.sh')
        os.fdopen(fd, 'w').write(pre_script)

        pre_script_on_rh6 = '''
ZSTACK_INSTALL_LOG='/tmp/zstack_installation.log'
rpm -qi python-crypto >/dev/null 2>&1
if [ $? -eq 0 ]; then
    echo "Management node remote installation failed. You need to manually remove python-crypto by \n\n \`rpm -ev python-crypto\` \n\n in remote management node; otherwise it will conflict with ansible's pycrypto." >>$ZSTACK_INSTALL_LOG
    exit 1
fi
'''
        t = string.Template(pre_script_on_rh6)

        fd, pre_script_on_rh6_path = tempfile.mkstemp(suffix='.sh')
        os.fdopen(fd, 'w').write(pre_script_on_rh6)

        def cleanup_pre_script():
            os.remove(pre_script_path)
            os.remove(pre_script_on_rh6_path)

        self.install_cleanup_routine(cleanup_pre_script)
        post_script = '''
set -e
filename=$apache_tomcat_zip_name
foldername="$${filename%.*}"
apache_path=$install_path/apache-tomcat
unzip $apache -d $install_path
ln -s $install_path/$$foldername $$apache_path
unzip $zstack -d $$apache_path/webapps/zstack

chmod a+x $$apache_path/bin/*

cat >> $$apache_path/bin/setenv.sh <<EOF
export CATALINA_OPTS=" -Djava.net.preferIPv4Stack=true -Dcom.sun.management.jmxremote=true"
EOF

install_script="$$apache_path/webapps/zstack/WEB-INF/classes/tools/install.sh"

eval "bash $$install_script zstack-ctl"
eval "bash $$install_script zstack-cli"

set +e
grep "ZSTACK_HOME" ~/.bashrc > /dev/null
if [ $$? -eq 0 ]; then
    sed -i "s#export ZSTACK_HOME=.*#export ZSTACK_HOME=$$apache_path/webapps/zstack#" ~/.bashrc
else
    echo "export ZSTACK_HOME=$$apache_path/webapps/zstack" >> ~/.bashrc
fi

which ansible-playbook &> /dev/null
if [ $$? -ne 0 ]; then
    pip install -i file://$pypi_path/simple --trusted-host localhost ansible
fi
'''
        t = string.Template(post_script)
        post_script = t.substitute({
            'install_path': args.install_path,
            'apache': os.path.join(args.install_path, apache_tomcat_zip_name),
            'zstack': os.path.join(args.install_path, 'zstack.war'),
            'apache_tomcat_zip_name': apache_tomcat_zip_name,
            'pypi_path': '/tmp/pypi/'
        })

        fd, post_script_path = tempfile.mkstemp(suffix='.sh')
        os.fdopen(fd, 'w').write(post_script)

        def cleanup_post_script():
            os.remove(post_script_path)

        self.install_cleanup_routine(cleanup_post_script)

        setup_account = '''id -u zstack >/dev/null 2>&1 || (useradd -d $install_path zstack && mkdir -p $install_path && chown -R zstack.zstack $install_path)
grep 'zstack' /etc/sudoers >/dev/null || echo 'zstack        ALL=(ALL)       NOPASSWD: ALL' >> /etc/sudoers
grep '^root' /etc/sudoers >/dev/null || echo 'root        ALL=(ALL)       NOPASSWD: ALL' >> /etc/sudoers
sed -i '/requiretty$$/d' /etc/sudoers
chown -R zstack.zstack $install_path
mkdir /home/zstack && chown -R zstack.zstack /home/zstack
zstack-ctl setenv ZSTACK_HOME=$install_path/apache-tomcat/webapps/zstack
'''
        t = string.Template(setup_account)
        setup_account = t.substitute({
            'install_path': args.install_path
        })

        fd, setup_account_path = tempfile.mkstemp()
        os.fdopen(fd, 'w').write(setup_account)

        def clean_up():
            os.remove(setup_account_path)

        self.install_cleanup_routine(clean_up)

        t = string.Template(yaml)
        if args.yum:
            yum_repo = 'true'
        else:
            yum_repo = 'false'
        yaml = t.substitute({
            'host': args.host,
            'install_path': args.install_path,
            'apache_path': apache_tomcat,
            'zstack_path': zstack,
            'pre_script': pre_script_path,
            'pre_script_on_rh6': pre_script_on_rh6_path,
            'post_script': post_script_path,
            'properties_file': ctl.properties_file_path,
            'apache_tomcat_zip_name': apache_tomcat_zip_name,
            'pypi_tar_path': pypi_tar_path,
            'pypi_tar_path_dest': '/tmp/pypi.tar.bz',
            'pypi_path': '/tmp/pypi/',
            'yum_folder': ctl.zstack_home,
            'yum_repo': yum_repo,
            'setup_account': setup_account_path
        })

        ansible(yaml, args.host, args.debug, args.ssh_key)
        info('successfully installed new management node on machine(%s)' % args.host)

class ShowConfiguration(Command):
    def __init__(self):
        super(ShowConfiguration, self).__init__()
        self.name = "show_configuration"
        self.description = "a shortcut that prints contents of zstack.properties to screen"
        ctl.register_command(self)

    def run(self, args):
        shell_no_pipe('cat %s' % ctl.properties_file_path)

class SetEnvironmentVariableCmd(Command):
    PATH = os.path.join(ctl.USER_ZSTACK_HOME_DIR, "zstack-ctl/ctl-env")

    def __init__(self):
        super(SetEnvironmentVariableCmd, self).__init__()
        self.name = "setenv"
        self.description = "set variables to zstack-ctl variable file at %s" % self.PATH
        ctl.register_command(self)

    def need_zstack_home(self):
        return False

    def run(self, args):
        if not ctl.extra_arguments:
            raise CtlError('please input variables that are in format of "key=value" split by space')

        if not os.path.isdir(ctl.USER_ZSTACK_HOME_DIR):
            raise CtlError('cannot find home directory(%s) of user "zstack"' % ctl.USER_ZSTACK_HOME_DIR)

        with use_user_zstack():
            path_dir = os.path.dirname(self.PATH)
            if not os.path.isdir(path_dir):
                os.makedirs(path_dir)

            with open(self.PATH, 'a'):
                # create the file if not existing
                pass

        env = PropertyFile(self.PATH)
        arg_str = ' '.join(ctl.extra_arguments)
        env.write_properties([arg_str.split('=', 1)])

class UnsetEnvironmentVariableCmd(Command):
    NAME = 'unsetenv'

    def __init__(self):
        super(UnsetEnvironmentVariableCmd, self).__init__()
        self.name = self.NAME
        self.description = (
            'unset variables in %s' % SetEnvironmentVariableCmd.PATH
        )
        ctl.register_command(self)

    def run(self, args):
        if not os.path.exists(SetEnvironmentVariableCmd.PATH):
            return

        if not ctl.extra_arguments:
            raise CtlError('please input a list of variable names you want to unset')

        env = PropertyFile(SetEnvironmentVariableCmd.PATH)
        env.delete_properties(ctl.extra_arguments)
        info('unset zstack environment variables: %s' % ctl.extra_arguments)


class GetEnvironmentVariableCmd(Command):
    NAME = 'getenv'

    def __init__(self):
        super(GetEnvironmentVariableCmd, self).__init__()
        self.name = self.NAME
        self.description = (
              "get variables from %s" % SetEnvironmentVariableCmd.PATH
        )
        ctl.register_command(self)

    def run(self, args):
        if not os.path.exists(SetEnvironmentVariableCmd.PATH):
            raise CtlError('cannot find the environment variable file at %s' % SetEnvironmentVariableCmd.PATH)

        ret = []
        if ctl.extra_arguments:
            env = PropertyFile(SetEnvironmentVariableCmd.PATH)
            for key in ctl.extra_arguments:
                value = env.read_property(key)
                if value:
                    ret.append('%s=%s' % (key, value))
        else:
            env = PropertyFile(SetEnvironmentVariableCmd.PATH)
            for k, v in env.read_all_properties():
                ret.append('%s=%s' % (k, v))

        info('\n'.join(ret))


class InstallWebUiCmd(Command):

    def __init__(self):
        super(InstallWebUiCmd, self).__init__()
        self.name = "install_ui"
        self.description = "install ZStack web UI"
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help='target host IP, for example, 192.168.0.212, to install ZStack web UI; if omitted, it will be installed on local machine')
        parser.add_argument('--ssh-key', help="the path of private key for SSH login $host; if provided, Ansible will use the specified key as private key to SSH login the $host", default=None)
        parser.add_argument('--yum', help="Use ZStack predefined yum repositories. The valid options include: alibase,aliepel,163base,ustcepel,zstack-local. NOTE: only use it when you know exactly what it does.", default=None)

    def _install_to_local(self):
        install_script = os.path.join(ctl.zstack_home, "WEB-INF/classes/tools/install.sh")
        if not os.path.isfile(install_script):
            raise CtlError('cannot find %s, please make sure you have installed ZStack management node' % install_script)

        info('found installation script at %s, start installing ZStack web UI' % install_script)
        shell('bash %s zstack-dashboard' % install_script)

    def run(self, args):
        if not args.host:
            self._install_to_local()
            return

        tools_path = os.path.join(ctl.zstack_home, "WEB-INF/classes/tools/")
        if not os.path.isdir(tools_path):
            raise CtlError('cannot find %s, please make sure you have installed ZStack management node' % tools_path)

        ui_binary = None
        for l in os.listdir(tools_path):
            if l.startswith('zstack_dashboard'):
                ui_binary = l
                break

        if not ui_binary:
            raise CtlError('cannot find zstack-dashboard package under %s, please make sure you have installed ZStack management node' % tools_path)

        ui_binary_path = os.path.join(tools_path, ui_binary)

        pypi_path = os.path.join(ctl.zstack_home, "static/pypi/")
        if not os.path.isdir(pypi_path):
            raise CtlError('cannot find %s, please make sure you have installed ZStack management node' % pypi_path)

        pypi_tar_path = os.path.join(ctl.zstack_home, "static/pypi.tar.bz")
        if not os.path.isfile(pypi_tar_path):
            static_path = os.path.join(ctl.zstack_home, "static")
            os.system('cd %s; tar jcf pypi.tar.bz pypi' % static_path)

        yaml = '''---
- hosts: $host
  remote_user: root

  vars:
      virtualenv_root: /var/lib/zstack/virtualenv/zstack-dashboard
      yum_repo: "$yum_repo"

  tasks:
    - name: pre-install script
      script: $pre_install_script

    - name: set RHEL7 yum repo
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '7'
      shell: echo -e "[zstack-local]\\nname=ZStack Local Yum Repo\\nbaseurl=file://$yum_folder/static/centos7_repo\\nenabled=0\\ngpgcheck=0\\n" > /etc/yum.repos.d/zstack-local.repo

    - name: set RHEL6 yum repo
      when: ansible_os_family == 'RedHat' and ansible_distribution_version >= '6' and ansible_distribution_version < '7'
      shell: echo -e "[zstack-local]\\nname=ZStack Local Yum Repo\\nbaseurl=file://$yum_folder/static/centos6_repo\\nenabled=0\\ngpgcheck=0\\n" > /etc/yum.repos.d/zstack-local.repo

    - name: install Python pip for RedHat OS from user defined repo
      when: ansible_os_family == 'RedHat' and yum_repo != 'false'
      shell: yum clean metadata; yum --disablerepo=* --enablerepo={{yum_repo}} --nogpgcheck install -y libselinux-python python-pip bzip2 python-devel gcc autoconf

    - name: install Python pip for RedHat OS from system repo
      when: ansible_os_family == 'RedHat' and yum_repo == 'false'
      shell: yum clean metadata; yum --nogpgcheck install -y libselinux-python python-pip bzip2 python-devel gcc autoconf

    - name: copy zstack-dashboard package
      copy: src=$src dest=$dest

    - name: copy pypi tar file
      copy: src=$pypi_tar_path dest=$pypi_tar_path_dest

    - name: untar pypi
      shell: "cd /tmp/; tar jxf $pypi_tar_path_dest"

    - name: install Python pip for Ubuntu
      when: ansible_os_family == 'Debian'
      apt: pkg=python-pip update_cache=yes

    - name: install pip from local source
      shell: "cd $pypi_path; pip install --ignore-installed pip*.tar.gz"

    - shell: virtualenv --version | grep "12.1.1"
      register: virtualenv_ret
      ignore_errors: True

    - name: install virtualenv
      pip: name=virtualenv version=12.1.1 extra_args="--ignore-installed --trusted-host localhost -i file://$pypi_path/simple"
      when: virtualenv_ret.rc != 0

    - name: create virtualenv
      shell: "rm -rf {{virtualenv_root}} && virtualenv {{virtualenv_root}}"

    - name: install zstack-dashboard
      pip: name=$dest extra_args="--trusted-host localhost -i file://$pypi_path/simple" virtualenv="{{virtualenv_root}}"

'''

        pre_script = '''
echo -e "#aliyun base\n[alibase]\nname=CentOS-\$releasever - Base - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$releasever/os/\$basearch/\ngpgcheck=0\nenabled=0\n \n#released updates \n[aliupdates]\nname=CentOS-\$releasever - Updates - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$releasever/updates/\$basearch/\nenabled=0\ngpgcheck=0\n \n[aliextras]\nname=CentOS-\$releasever - Extras - mirrors.aliyun.com\nfailovermethod=priority\nbaseurl=http://mirrors.aliyun.com/centos/\$releasever/extras/\$basearch/\nenabled=0\ngpgcheck=0\n \n[aliepel]\nname=Extra Packages for Enterprise Linux \$releasever - \$basearce - mirrors.aliyun.com\nbaseurl=http://mirrors.aliyun.com/epel/\$releasever/\$basearch\nfailovermethod=priority\nenabled=0\ngpgcheck=0\n" > /etc/yum.repos.d/zstack-aliyun-yum.repo

echo -e "#163 base\n[163base]\nname=CentOS-\$releasever - Base - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$releasever/os/\$basearch/\ngpgcheck=0\nenabled=0\n \n#released updates \n[163updates]\nname=CentOS-\$releasever - Updates - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$releasever/updates/\$basearch/\nenabled=0\ngpgcheck=0\n \n#additional packages that may be useful\n[163extras]\nname=CentOS-\$releasever - Extras - mirrors.163.com\nfailovermethod=priority\nbaseurl=http://mirrors.163.com/centos/\$releasever/extras/\$basearch/\nenabled=0\ngpgcheck=0\n \n[ustcepel]\nname=Extra Packages for Enterprise Linux \$releasever - \$basearch - ustc \nbaseurl=http://centos.ustc.edu.cn/epel/\$releasever/\$basearch\nfailovermethod=priority\nenabled=0\ngpgcheck=0\n" > /etc/yum.repos.d/zstack-163-yum.repo
'''
        fd, pre_script_path = tempfile.mkstemp()
        os.fdopen(fd, 'w').write(pre_script)

        def cleanup_prescript():
            os.remove(pre_script_path)

        self.install_cleanup_routine(cleanup_prescript)
        t = string.Template(yaml)
        if args.yum:
            yum_repo = args.yum
        else:
            yum_repo = 'false'
        yaml = t.substitute({
            "src": ui_binary_path,
            "dest": os.path.join('/tmp', ui_binary),
            "host": args.host,
            'pre_install_script': pre_script_path,
            'pypi_tar_path': pypi_tar_path,
            'pypi_tar_path_dest': '/tmp/pypi.tar.bz',
            'pypi_path': '/tmp/pypi/',
            'yum_folder': ctl.zstack_home,
            'yum_repo': yum_repo
        })

        ansible(yaml, args.host, ssh_key=args.ssh_key)

class BootstrapCmd(Command):
    def __init__(self):
        super(BootstrapCmd, self).__init__()
        self.name = 'bootstrap'
        self.description = (
            'create user and group of "zstack" and add "zstack" to sudoers;'
            '\nthis command is only needed by installation script'
            ' and users that install ZStack manually'
        )
        ctl.register_command(self)

    def need_zstack_user(self):
        return False

    def run(self, args):
        shell('id -u zstack 2>/dev/null || (useradd -d %s zstack -s /bin/false && mkdir -p %s && chown -R zstack.zstack %s)' % (ctl.USER_ZSTACK_HOME_DIR, ctl.USER_ZSTACK_HOME_DIR, ctl.USER_ZSTACK_HOME_DIR))
        shell("grep 'zstack' /etc/sudoers || echo 'zstack        ALL=(ALL)       NOPASSWD: ALL' >> /etc/sudoers")
        shell('mkdir -p %s && chown zstack:zstack %s' % (ctl.USER_ZSTACK_HOME_DIR, ctl.USER_ZSTACK_HOME_DIR))


class UpgradeManagementNodeCmd(Command):
    def __init__(self):
        super(UpgradeManagementNodeCmd, self).__init__()
        self.name = "upgrade_management_node"
        self.description = 'upgrade the management node to a specified version'
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help='IP or DNS name of the machine to upgrade the management node', default=None)
        parser.add_argument('--war-file', help='path to zstack.war. A HTTP/HTTPS url or a path to a local zstack.war', required=True)
        parser.add_argument('--debug', help="open Ansible debug option", action="store_true", default=False)
        parser.add_argument('--ssh-key', help="the path of private key for SSH login $host; if provided, Ansible will use the specified key as private key to SSH login the $host", default=None)

    def run(self, args):
        error_if_tool_is_missing('unzip')
        need_download = args.war_file.startswith('http')
        if need_download:
            error_if_tool_is_missing('wget')

        upgrade_tmp_dir = os.path.join(ctl.USER_ZSTACK_HOME_DIR, 'upgrade', time.strftime('%Y-%m-%d-%H-%M-%S', time.gmtime()))
        shell('mkdir -p %s' % upgrade_tmp_dir)

        property_file_backup_path = os.path.join(upgrade_tmp_dir, 'zstack.properties')

        class NewWarFilePath(object):
            self.path = None

        new_war = NewWarFilePath()

        if not need_download:
            new_war.path = expand_path(args.war_file)
            if not os.path.exists(new_war.path):
                raise CtlError('%s not found' % new_war.path)

        def local_upgrade():
            def backup():
                ctl.internal_run('save_config', '--save-to %s' % os.path.dirname(property_file_backup_path))

                shell('cp -r %s %s' % (ctl.zstack_home, upgrade_tmp_dir))
                info('backup %s to %s' % (ctl.zstack_home, upgrade_tmp_dir))

            def download_war_if_needed():
                if need_download:
                    new_war.path = os.path.join(upgrade_tmp_dir, 'new', 'zstack.war')
                    shell_no_pipe('wget --no-check-certificate %s -O %s' % (args.war_file, new_war.path))
                    info('downloaded new zstack.war to %s' % new_war.path)

            def stop_node():
                info('start to stop the management node ...')
                ctl.internal_run('stop_node')

            def upgrade():
                info('start to upgrade the management node ...')
                shell('rm -rf %s' % ctl.zstack_home)
                if ctl.zstack_home.endswith('/'):
                    webapp_dir = os.path.dirname(os.path.dirname(ctl.zstack_home))
                else:
                    webapp_dir = os.path.dirname(ctl.zstack_home)

                shell('cp %s %s' % (new_war.path, webapp_dir))
                ShellCmd('unzip %s -d zstack' % os.path.basename(new_war.path), workdir=webapp_dir)()

            def restore_config():
                info('restoring the zstack.properties ...')
                ctl.internal_run('restore_config', '--restore-from %s' % property_file_backup_path)

            def install_tools():
                info('upgrading zstack-cli, zstack-ctl; this may cost several minutes ...')
                install_script = os.path.join(ctl.zstack_home, "WEB-INF/classes/tools/install.sh")
                if not os.path.isfile(install_script):
                    raise CtlError('cannot find %s, please make sure you have installed ZStack management node' % install_script)

                shell("bash %s zstack-cli" % install_script)
                shell("bash %s zstack-ctl" % install_script)
                info('successfully upgraded zstack-cli, zstack-ctl')

            def save_new_war():
                sdir = os.path.join(ctl.zstack_home, "../../../")
                shell('yes | cp %s %s' % (new_war.path, sdir))

            def chown_to_zstack():
                info('change permission to user zstack')
                shell('chown -R zstack:zstack %s' % os.path.join(ctl.zstack_home, '../../'))

            backup()
            download_war_if_needed()
            stop_node()
            upgrade()
            restore_config()
            install_tools()
            save_new_war()
            chown_to_zstack()

            info('----------------------------------------------\n'
                 'Successfully upgraded the ZStack management node to a new version.\n'
                 'We backup the old zstack as follows:\n'
                 '\tzstack.properties: %s\n'
                 '\tzstack folder: %s\n'
                 'Please test your new ZStack. If everything is OK and stable, you can manually delete those backup by deleting %s.\n'
                 'Otherwise you can use them to rollback to the previous version\n'
                 '-----------------------------------------------\n' %
                 (property_file_backup_path, os.path.join(upgrade_tmp_dir, 'zstack'), upgrade_tmp_dir))

        def remote_upgrade():
            need_copy = 'true'
            src_war = new_war.path
            dst_war = '/tmp/zstack.war'

            if need_download:
                need_copy = 'false'
                src_war = args.war_file
                dst_war = args.war_file

            upgrade_script = '''
zstack-ctl upgrade_management_node --war-file=$war_file
if [ $$? -ne 0 ]; then
    echo 'failed to upgrade the remote management node'
    exit 1
fi

if [ "$need_copy" == "true" ]; then
    rm -f $war_file
fi
'''
            t = string.Template(upgrade_script)
            upgrade_script = t.substitute({
                'war_file': dst_war,
                'need_copy': need_copy
            })

            fd, upgrade_script_path = tempfile.mkstemp(suffix='.sh')
            os.fdopen(fd, 'w').write(upgrade_script)

            def cleanup_upgrade_script():
                os.remove(upgrade_script_path)

            self.install_cleanup_routine(cleanup_upgrade_script)

            yaml = '''---
- hosts: $host
  remote_user: root

  vars:
    need_copy: "$need_copy"

  tasks:
    - name: copy zstack.war to remote
      copy: src=$src_war dest=$dst_war
      when: need_copy == 'true'

    - name: upgrade management node
      script: $upgrade_script
      register: output
      ignore_errors: yes

    - name: failure
      fail: msg="failed to upgrade the remote management node. {{ output.stdout }} {{ output.stderr }}"
      when: output.rc != 0
'''
            t = string.Template(yaml)
            yaml = t.substitute({
                "src_war": src_war,
                "dst_war": dst_war,
                "host": args.host,
                "need_copy": need_copy,
                "upgrade_script": upgrade_script_path
            })

            info('start to upgrade the remote management node; the process may cost several minutes ...')
            ansible(yaml, args.host, args.debug, ssh_key=args.ssh_key)
            info('upgraded the remote management node successfully')


        if args.host:
            remote_upgrade()
        else:
            local_upgrade()


class UpgradeDbCmd(Command):
    def __init__(self):
        super(UpgradeDbCmd, self).__init__()
        self.name = 'upgrade_db'
        self.description = (
            'upgrade the database from current version to a new version'
        )
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--force', help='bypass management nodes status check.'
                            '\nNOTE: only use it when you know exactly what it does', action='store_true', default=False)
        parser.add_argument('--no-backup', help='do NOT backup the database. If the database is very large and you have manually backup it, using this option will fast the upgrade process. [DEFAULT] false', default=False)
        parser.add_argument('--dry-run', help='Check if db could be upgraded. [DEFAULT] not set', action='store_true', default=False)

    def run(self, args):
        error_if_tool_is_missing('mysqldump')
        error_if_tool_is_missing('mysql')

        db_url = ctl.get_db_url()
        if 'zstack' not in db_url:
            db_url = '%s/zstack' % db_url.rstrip('/')

        db_hostname, db_port, db_user, db_password = ctl.get_database_portal()

        flyway_path = os.path.join(ctl.zstack_home, 'WEB-INF/classes/tools/flyway-3.2.1/flyway')
        if not os.path.exists(flyway_path):
            raise CtlError('cannot find %s. Have you run upgrade_management_node?' % flyway_path)

        upgrading_schema_dir = os.path.join(ctl.zstack_home, 'WEB-INF/classes/db/upgrade/')
        if not os.path.exists(upgrading_schema_dir):
            raise CtlError('cannot find %s. Have you run upgrade_management_node?' % upgrading_schema_dir)

        ctl.check_if_management_node_has_stopped(args.force)

        if args.dry_run:
            info('Dry run finished. Database could be upgraded. ')
            return True

        def backup_current_database():
            if args.no_backup:
                return

            info('start to backup the database ...')

            db_backup_path = os.path.join(ctl.USER_ZSTACK_HOME_DIR, 'db_backup', time.strftime('%Y-%m-%d-%H-%M-%S', time.gmtime()), 'backup.sql')
            shell('mkdir -p %s' % os.path.dirname(db_backup_path))
            if db_password:
                shell('mysqldump -u %s -p%s --host %s --port %s zstack > %s' % (db_user, db_password, db_hostname, db_port, db_backup_path))
            else:
                shell('mysqldump -u %s --host %s --port %s zstack > %s' % (db_user, db_hostname, db_port, db_backup_path))

            info('successfully backup the database to %s' % db_backup_path)

        def create_schema_version_table_if_needed():
            if db_password:
                out = shell('''mysql -u %s -p%s --host %s --port %s -t zstack -e "show tables like 'schema_version'"''' %
                            (db_user, db_password, db_hostname, db_port))
            else:
                out = shell('''mysql -u %s --host %s --port %s -t zstack -e "show tables like 'schema_version'"''' %
                            (db_user, db_hostname, db_port))

            if 'schema_version' in out:
                return

            info('version table "schema_version" is not existing; initializing a new version table first')

            if db_password:
                shell_no_pipe('bash %s baseline -baselineVersion=0.6 -baselineDescription="0.6 version" -user=%s -password=%s -url=%s' %
                      (flyway_path, db_user, db_password, db_url))
            else:
                shell_no_pipe('bash %s baseline -baselineVersion=0.6 -baselineDescription="0.6 version" -user=%s -url=%s' %
                      (flyway_path, db_user, db_url))

        def migrate():
            schema_path = 'filesystem:%s' % upgrading_schema_dir
            if db_password:
                shell_no_pipe('bash %s migrate -user=%s -password=%s -url=%s -locations=%s' % (flyway_path, db_user, db_password, db_url, schema_path))
            else:
                shell_no_pipe('bash %s migrate -user=%s -url=%s -locations=%s' % (flyway_path, db_user, db_url, schema_path))

            info('Successfully upgraded the database to the latest version.\n')

        backup_current_database()
        create_schema_version_table_if_needed()
        migrate()

class UpgradeCtlCmd(Command):
    def __init__(self):
        super(UpgradeCtlCmd, self).__init__()
        self.name = 'upgrade_ctl'
        self.description = (
            'upgrade the zstack-ctl to a new version'
        )
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--package', help='the path to the new zstack-ctl package', required=True)

    def run(self, args):
        error_if_tool_is_missing('pip')

        path = expand_path(args.package)
        if not os.path.exists(path):
            raise CtlError('%s not found' % path)

        pypi_path = os.path.join(ctl.zstack_home, "static/pypi/")
        if not os.path.isdir(pypi_path):
            raise CtlError('cannot find %s, please make sure you have installed ZStack management node' % pypi_path)

        install_script = '''set -e
which virtualenv &>/dev/null
if [ $$? != 0 ]; then
    pip install -i file://$pypi_path/simple --trusted-host localhost virtualenv
fi

CTL_VIRENV_PATH=/var/lib/zstack/virtualenv/zstackctl
rm -rf $$CTL_VIRENV_PATH
virtualenv $$CTL_VIRENV_PATH
. $$CTL_VIRENV_PATH/bin/activate

pip install -i file://$pypi_path/simple --trusted-host --ignore-installed $package || exit 1
chmod +x /usr/bin/zstack-ctl
'''

        script(install_script, {"pypi_path": pypi_path, "package": args.package})
        info('successfully upgraded zstack-ctl to %s' % args.package)

class RollbackManagementNodeCmd(Command):
    def __init__(self):
        super(RollbackManagementNodeCmd, self).__init__()
        self.name = "rollback_management_node"
        self.description = "rollback the management node to a previous version if the upgrade fails"
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help='the IP or DNS name of machine to rollback the management node')
        parser.add_argument('--war-file', help='path to zstack.war. A HTTP/HTTPS url or a path to a local zstack.war', required=True)
        parser.add_argument('--debug', help="open Ansible debug option", action="store_true", default=False)
        parser.add_argument('--ssh-key', help="the path of private key for SSH login $host; if provided, Ansible will use the specified key as private key to SSH login the $host", default=None)
        parser.add_argument('--property-file', help="the path to zstack.properties. If omitted, the current zstack.properties will be used", default=None)

    def run(self, args):
        error_if_tool_is_missing('unzip')

        rollback_tmp_dir = os.path.join(ctl.USER_ZSTACK_HOME_DIR, 'rollback', time.strftime('%Y-%m-%d-%H-%M-%S', time.gmtime()))
        shell('mkdir -p %s' % rollback_tmp_dir)
        need_download = args.war_file.startswith('http')

        class Info(object):
            def __init__(self):
                self.war_path = None
                self.property_file = None

        rollbackinfo = Info()

        def local_rollback():
            def backup_current_zstack():
                info('start to backup the current zstack ...')
                shell('cp -r %s %s' % (ctl.zstack_home, rollback_tmp_dir))
                info('backup %s to %s' % (ctl.zstack_home, rollback_tmp_dir))
                info('successfully backup the current zstack to %s' % os.path.join(rollback_tmp_dir, os.path.basename(ctl.zstack_home)))

            def download_war_if_needed():
                if need_download:
                    rollbackinfo.war_path = os.path.join(rollback_tmp_dir, 'zstack.war')
                    shell_no_pipe('wget --no-check-certificate %s -O %s' % (args.war_file, rollbackinfo.war_path))
                    info('downloaded zstack.war to %s' % rollbackinfo.war_path)
                else:
                    rollbackinfo.war_path = expand_path(args.war_file)
                    if not os.path.exists(rollbackinfo.war_path):
                        raise CtlError('%s not found' % rollbackinfo.war_path)

            def save_property_file_if_needed():
                if not args.property_file:
                    ctl.internal_run('save_config', '--save-to %s' % rollback_tmp_dir)
                    rollbackinfo.property_file = os.path.join(rollback_tmp_dir, 'zstack.properties')
                else:
                    rollbackinfo.property_file = args.property_file
                    if not os.path.exists(rollbackinfo.property_file):
                        raise CtlError('%s not found' % rollbackinfo.property_file)

            def stop_node():
                info('start to stop the management node ...')
                ctl.internal_run('stop_node')

            def rollback():
                info('start to rollback the management node ...')
                shell('rm -rf %s' % ctl.zstack_home)
                shell('unzip %s -d %s' % (rollbackinfo.war_path, ctl.zstack_home))

            def restore_config():
                info('restoring the zstack.properties ...')
                ctl.internal_run('restore_config', '--restore-from %s' % rollbackinfo.property_file)

            def install_tools():
                info('rollback zstack-cli, zstack-ctl to the previous version. This may cost several minutes ...')
                install_script = os.path.join(ctl.zstack_home, "WEB-INF/classes/tools/install.sh")
                if not os.path.isfile(install_script):
                    raise CtlError('cannot find %s, please make sure you have installed ZStack management node' % install_script)

                shell("bash %s zstack-cli" % install_script)
                shell("bash %s zstack-ctl" % install_script)
                info('successfully upgraded zstack-cli, zstack-ctl')

            backup_current_zstack()
            download_war_if_needed()
            save_property_file_if_needed()
            stop_node()
            rollback()
            restore_config()
            install_tools()

            info('----------------------------------------------\n'
                 'Successfully rollback the ZStack management node to a previous version.\n'
                 'We backup the current zstack as follows:\n'
                 '\tzstack.properties: %s\n'
                 '\tzstack folder: %s\n'
                 'Please test your ZStack. If everything is OK and stable, you can manually delete those backup by deleting %s.\n'
                 '-----------------------------------------------\n' %
                 (rollbackinfo.property_file, os.path.join(rollback_tmp_dir, os.path.basename(ctl.zstack_home)), rollback_tmp_dir))

        def remote_rollback():
            error_if_tool_is_missing('wget')

            need_copy = 'true'
            src_war = rollbackinfo.war_path
            dst_war = '/tmp/zstack.war'

            if need_download:
                need_copy = 'false'
                src_war = args.war_file
                dst_war = args.war_file

            rollback_script = '''
zstack-ctl rollback_management_node --war-file=$war_file
if [ $$? -ne 0 ]; then
    echo 'failed to rollback the remote management node'
    exit 1
fi

if [ "$need_copy" == "true" ]; then
    rm -f $war_file
fi
'''

            t = string.Template(rollback_script)
            rollback_script = t.substitute({
                'war_file': dst_war,
                'need_copy': need_copy
            })

            fd, rollback_script_path = tempfile.mkstemp(suffix='.sh')
            os.fdopen(fd, 'w').write(rollback_script)

            def cleanup_rollback_script():
                os.remove(rollback_script_path)

            self.install_cleanup_routine(cleanup_rollback_script)

            yaml = '''---
- hosts: $host
  remote_user: root

  vars:
    need_copy: "$need_copy"

  tasks:
    - name: copy zstack.war to remote
      copy: src=$src_war dest=$dst_war
      when: need_copy == 'true'

    - name: rollback the management node
      script: $rollback_script
      register: output
      ignore_errors: yes

    - name: failure
      fail: msg="failed to rollback the remote management node. {{ output.stdout }} {{ output.stderr }}"
      when: output.rc != 0
'''

            t = string.Template(yaml)
            yaml = t.substitute({
                "src_war": src_war,
                "dst_war": dst_war,
                "host": args.host,
                "need_copy": need_copy,
                "rollback_script": rollback_script_path
            })

            info('start to rollback the remote management node; the process may cost several minutes ...')
            ansible(yaml, args.host, args.debug, ssh_key=args.ssh_key)
            info('successfully rollback the remote management node')

        if args.host:
            remote_rollback()
        else:
            local_rollback()

class RollbackDatabaseCmd(Command):
    def __init__(self):
        super(RollbackDatabaseCmd, self).__init__()
        self.name = 'rollback_db'
        self.description = "rollback the database to the previous version if the upgrade fails"
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--db-dump', help="the previous database dump file", required=True)
        parser.add_argument('--root-password', help="the password for mysql root user. [DEFAULT] empty password")
        parser.add_argument('--force', help='bypass management nodes status check.'
                            '\nNOTE: only use it when you know exactly what it does', action='store_true', default=False)

    def run(self, args):
        error_if_tool_is_missing('mysql')

        ctl.check_if_management_node_has_stopped(args.force)

        if not os.path.exists(args.db_dump):
            raise CtlError('%s not found' % args.db_dump)

        host, port, _, _ = ctl.get_database_portal()

        if args.root_password:
            cmd = ShellCmd('mysql -u root -p%s --host %s --port %s -e "select 1"' % (args.root_password, host, port))
        else:
            cmd = ShellCmd('mysql -u root --host %s --port %s -e "select 1"' % (host, port))

        cmd(False)
        if cmd.return_code != 0:
            error_not_exit('failed to test the mysql server. You may have provided a wrong password of the root user. Please use --root-password to provide the correct password')
            cmd.raise_error()

        info('start to rollback the database ...')

        if args.root_password:
            shell('mysql -u root -p%s --host %s --port %s -t zstack < %s' % (args.root_password, host, port, args.db_dump))
        else:
            shell('mysql -u root --host %s --port %s -t zstack < %s' % (host, port, args.db_dump))

        info('successfully rollback the database to the dump file %s' % args.db_dump)

class StopUiCmd(Command):
    def __init__(self):
        super(StopUiCmd, self).__init__()
        self.name = 'stop_ui'
        self.description = "stop UI server on the local or remote host"
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help="UI server IP. [DEFAULT] localhost", default='localhost')

    def _remote_stop(self, host):
        cmd = '/etc/init.d/zstack-dashboard stop'
        ssh_run_no_pipe(host, cmd)

    def run(self, args):
        if args.host != 'localhost':
            self._remote_stop(args.host)
            return

        pidfile = '/var/run/zstack/zstack-dashboard.pid'
        if os.path.exists(pidfile):
            with open(pidfile, 'r') as fd:
                pid = fd.readline()
                pid = pid.strip(' \t\n\r')
                shell('kill %s >/dev/null 2>&1' % pid, is_exception=False)

        def stop_all():
            pid = find_process_by_cmdline('zstack_dashboard')
            if pid:
                shell('kill -9 %s >/dev/null 2>&1' % pid)
                stop_all()
            else:
                return

        stop_all()
        info('successfully stopped the UI server')

class UiStatusCmd(Command):
    def __init__(self):
        super(UiStatusCmd, self).__init__()
        self.name = "ui_status"
        self.description = "check the UI server status on the local or remote host."
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help="UI server IP. [DEFAULT] localhost", default='localhost')

    def _remote_status(self, host):
        cmd = '/etc/init.d/zstack-dashboard status'
        ssh_run_no_pipe(host, cmd)

    def run(self, args):
        if args.host != 'localhost':
            self._remote_status(args.host)
            return

        pidfile = '/var/run/zstack/zstack-dashboard.pid'
        if os.path.exists(pidfile):
            with open(pidfile, 'r') as fd:
                pid = fd.readline()
                pid = pid.strip(' \t\n\r')
                check_pid_cmd = ShellCmd('ps -p %s > /dev/null' % pid)
                check_pid_cmd(is_exception=False)
                if check_pid_cmd.return_code == 0:
                    info('%s: [PID:%s]' % (colored('Running', 'green'), pid))
                    return

        pid = find_process_by_cmdline('zstack_dashboard')
        if pid:
            info('%s: [PID: %s]' % (colored('Zombie', 'yellow'), pid))
        else:
            info('%s: [PID: %s]' % (colored('Stopped', 'red'), pid))

class InstallLicenseCmd(Command):
    def __init__(self):
        super(InstallLicenseCmd, self).__init__()
        self.name = "install_license"
        self.description = "install zstack license"
        ctl.register_command(self)

    def install_argparse_arguments(self, parser):
        parser.add_argument('--license', '-l', help="path to the license file", required=True)
        parser.add_argument('--prikey', help="[OPTIONAL] the path to the private key used to generate license request")

    def run(self, args):
        lpath = expand_path(args.license)
        if not os.path.isfile(lpath):
            raise CtlError('cannot find the license file at %s' % args.license)

        ppath = None
        if args.prikey:
            ppath = expand_path(args.prikey)
            if not os.path.isfile(ppath):
                raise CtlError('cannot find the private key file at %s' % args.prikey)

        license_folder = os.path.join(ctl.USER_ZSTACK_HOME_DIR, 'license')
        shell('''su - zstack -c "mkdir -p %s"''' % license_folder)
        shell('''yes | cp %s %s/license.txt''' % (lpath, license_folder))
        shell('''chown zstack:zstack %s/license.txt''' % license_folder)
        info("successfully installed the license file to %s/license.txt" % license_folder)
        if ppath:
            shell('''yes | cp %s %s/pri.key''' % (ppath, license_folder))
            shell('''chown zstack:zstack %s/pri.key''' % license_folder)
            info("successfully installed the private key file to %s/pri.key" % license_folder)


class StartUiCmd(Command):
    PID_FILE = '/var/run/zstack/zstack-dashboard.pid'

    def __init__(self):
        super(StartUiCmd, self).__init__()
        self.name = "start_ui"
        self.description = "start UI server on the local or remote host"
        ctl.register_command(self)
        if not os.path.exists(os.path.dirname(self.PID_FILE)):
            shell("mkdir -p %s" % os.path.dirname(self.PID_FILE))
            shell("mkdir -p /var/log/zstack")

    def install_argparse_arguments(self, parser):
        parser.add_argument('--host', help="UI server IP. [DEFAULT] localhost", default='localhost')

    def _remote_start(self, host, params):
        cmd = '/etc/init.d/zstack-dashboard start --rabbitmq %s' % params
        ssh_run_no_pipe(host, cmd)
        info('successfully start the UI server on the remote host[%s]' % host)

    def _check_status(self):
        if os.path.exists(self.PID_FILE):
            with open(self.PID_FILE, 'r') as fd:
                pid = fd.readline()
                pid = pid.strip(' \t\n\r')
                check_pid_cmd = ShellCmd('ps -p %s > /dev/null' % pid)
                check_pid_cmd(is_exception=False)
                if check_pid_cmd.return_code == 0:
                    info('UI server is still running[PID:%s]' % pid)
                    return False

        pid = find_process_by_cmdline('zstack_dashboard')
        if pid:
            info('found a zombie UI server[PID:%s], kill it and start a new one' % pid)
            shell('kill -9 %s > /dev/null' % pid)

        return True

    def run(self, args):
        ips = ctl.read_property_list("CloudBus.serverIp.")
        if not ips:
            raise CtlError('no RabbitMQ IPs found in %s. The IPs should be configured as CloudBus.serverIp.0, CloudBus.serverIp.1 ... CloudBus.serverIp.N' % ctl.properties_file_path)

        ips = [v for k, v in ips]

        username = ctl.read_property("CloudBus.rabbitmqUsername")
        password = ctl.read_property("CloudBus.rabbitmqPassword")
        if username and not password:
            raise CtlError('CloudBus.rabbitmqUsername is configured but CloudBus.rabbitmqPassword is not. They must be both set or not set. Check %s' % ctl.properties_file_path)
        if not username and password:
            raise CtlError('CloudBus.rabbitmqPassword is configured but CloudBus.rabbitmqUsername is not. They must be both set or not set. Check %s' % ctl.properties_file_path)

        if username and password:
            urls = ["%s:%s@%s" % (username, password, ip) for ip in ips]
        else:
            urls = ips

        param = ','.join(urls)

        if args.host != 'localhost':
            self._remote_start(args.host, param)
            return

        virtualenv = '/var/lib/zstack/virtualenv/zstack-dashboard'
        if not os.path.exists(virtualenv):
            raise CtlError('%s not found. Are you sure the UI server is installed on %s?' % (virtualenv, args.host))

        if not self._check_status():
            return

        shell('iptables-save | grep -- "-A INPUT -p tcp -m tcp --dport 5000 -j ACCEPT" > /dev/null || iptables -I INPUT -p tcp -m tcp --dport 5000 -j ACCEPT')

        scmd = '. %s/bin/activate\nnohup python -c "from zstack_dashboard import web; web.main()" --rabbitmq %s >/var/log/zstack/zstack-dashboard.log 2>&1 </dev/null &' % (virtualenv, param)
        script(scmd, no_pipe=True)

        @loop_until_timeout(5, 0.5)
        def write_pid():
            pid = find_process_by_cmdline('zstack_dashboard')
            if pid:
                with open(self.PID_FILE, 'w') as fd:
                    fd.write(str(pid))
                return True
            else:
                return False

        write_pid()
        pid = find_process_by_cmdline('zstack_dashboard')
        info('successfully started UI server on the local host, PID[%s]' % pid)

def main():
    BootstrapCmd()
    DeployDBCmd()
    ShowStatusCmd()
    TailLogCmd()
    StartCmd()
    StopCmd()
    SaveConfigCmd()
    RestoreConfigCmd()
    ConfigureCmd()
    InstallDbCmd()
    InstallRabbitCmd()
    InstallManagementNodeCmd()
    ShowConfiguration()
    SetEnvironmentVariableCmd()
    GetEnvironmentVariableCmd()
    InstallWebUiCmd()
    UpgradeManagementNodeCmd()
    UpgradeDbCmd()
    UpgradeCtlCmd()
    RollbackManagementNodeCmd()
    RollbackDatabaseCmd()
    StartUiCmd()
    StopUiCmd()
    UiStatusCmd()
    InstallCassandraCmd()
    InstallKairosdbCmd()
    CassandraCmd()
    KairosdbCmd()
    StartAllCmd()
    StopAllCmd()
    InstallLicenseCmd()
    UnsetEnvironmentVariableCmd()

    try:
        ctl.run()
    except CtlError as e:
        if ctl.verbose:
            error_not_exit(traceback.format_exc())
        error(str(e))

if __name__ == '__main__':
    main()

