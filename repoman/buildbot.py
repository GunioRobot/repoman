from simplejson import dumps
from webob import Response
from pycurl import Curl

from subprocess import Popen, PIPE, STDOUT
from multiprocessing import Process, Queue
from traceback import format_exc
from urlparse import urljoin
from time import sleep
import logging
import tarfile
import os
import os.path
import urllib
import uuid
import sys
import os

from config import conf
from common import RequestHandler

class GitRepository(object):
    def __init__(self, path=None):
        self.path = path

    def _cmd(self, args, shell=False):
        try:
            os.chdir(self.path)
        except: pass
        logging.debug('cwd: %s    exec: %s' % (os.getcwd(), ' '.join(args)))
        p = Popen(args, stdout=PIPE, stderr=PIPE, shell=shell)
        ret = (p.communicate(), p.returncode)
        if ret[0][0]:
            logging.debug('\n'.join(ret[0]))
        return ret

    def _git(self, args):
        return self._cmd(['/usr/bin/git'] + args)

    def clone(self, gitpath):
        return self._git(['clone', gitpath, self.path])

    def checkout(self, ref):
        return self._git(['checkout', ref])

    def submodule_init(self):
        return self._git(['submodule', 'init'])

    def submodule_update(self):
        return self._git(['submodule', 'update'])

    def ls_remote(self, gitpath):
        output, retcode = self._git(['ls-remote', '--heads', '--tags', gitpath])
        stdout, stderr = output
        return [x.split('\t') for x in stdout.split('\n') if x]

    def show_ref(self):
        output, retcode = self._git(['show-ref', '--heads', '--tags'])
        stdout, stderr = output
        return [x.split(' ', 1) for x in stdout.split('\n') if x]

    def build(self, signkey, pbuilderrc, resultsdir, logfile):
        if 'refs/heads/upstream' in [x[1] for x in self.show_ref()]:
            cmd = ['/usr/bin/git-buildpackage', '--git-sign', '--git-cleaner="fakeroot debian/rules clean"', '--git-keyid="%s"' % signkey, '--git-builder="pdebuild --debsign-k %s --auto-debsign --configfile %s --debbuildopts "-i.git -I.git -sa" --buildresult %s' % (signkey, pbuilderrc, resultsdir)]
        else:
            cmd = ['/usr/bin/pdebuild', '--debsign-k', signkey, '--auto-debsign', '--debbuildopts', '-i\.git -I.git -sa', '--configfile', pbuilderrc, '--buildresult', resultsdir]

        buildlog = file(logfile, 'a+')
        p = Popen(cmd, stdout=buildlog, stderr=STDOUT)
        return p.wait()

class PackageHandler(RequestHandler):
    def get(self, gitpath, gitrepo):
        gitpath = os.path.join(conf('buildbot.gitpath.%s' % gitpath), gitrepo)

        repo = GitRepository()
        refs = repo.ls_remote(gitpath)
        return Response(status=200, body=dumps(refs))

    def post(self, gitpath, gitrepo):
        if not 'ref' in self.request.params:
            return Response(status=400, body='Required parameter "ref" is missing. You must pass a git tag, branch, or commit ID to be built.\n')

        gitpath = os.path.join(conf('buildbot.gitpath.%s' % gitpath), gitrepo)
        ref = self.request.params['ref']
        cburl = self.request.params.get('cburl', None)
        submodules = self.request.params.get('submodules', None)
        environment = self.request.params.get('environment', 'stable')

        buildid = uuid.uuid4().hex

        buildq.put((gitpath, ref, buildid, environment, cburl, submodules))
        return Response(status=200, body=buildid + '\n')

class RepoListHandler(RequestHandler):
    def get(self, gitpath):
        try:
            gitindex = conf('buildbot.gitindex.%s' % gitpath)
        except KeyError:
            return Response(status=404, body='Unknown git path')
        response = urllib.urlopen(gitindex)
        index = response.read()
        index = [x.strip('\r\n ').split(' ')[0].rsplit('.')[0] for x in index.split('\n') if x.strip('\r\n ')]
        return Response(status=200, body=dumps(index))

class TarballHandler(RequestHandler):
    def get(self, buildid):
        builddir = os.path.join(conf('buildbot.buildpath'), buildid)
        if not os.path.exists(builddir):
            return Response(status=404, body='The build ID does not exist.\n')

        tarpath = os.path.join(builddir, 'package.tar.gz')
        if not os.path.exists(tarpath):
            return Response(status=400, body='The build is not done yet.\n')
        else:
            fd = file(tarpath, 'rb')
            data = fd.read()
            fd.close()
            return Response(status=200, body=data, content_type='application/x-tar-gz')

class StatusHandler(RequestHandler):
    def get(self, buildid):
        builddir = os.path.join(conf('buildbot.buildpath'), buildid)
        if not os.path.exists(builddir):
            return Response(status=404, body='The build ID does not exist.\n')

        try:
            log = file('%s/build.log' % builddir, 'r').read()
        except:
            log = ''
        if not os.path.exists(builddir + '/package.tar.gz'):
            return Response(status=400, body='The build is not done yet.\n' + log)
        else:
            return Response(status=200, body='Build complete.\n' + log)

def buildlog(buildid, message):
    filename = os.path.join(conf('buildbot.buildpath'), '%s/build.log' % buildid)
    fd = file(filename, 'a+')
    fd.write(message + '\n')
    fd.close()
    logging.debug(message)

def build_thread(gitpath, ref, buildid, environment, cburl=None, submodules=False):
    tmpdir = os.path.join(conf('buildbot.buildpath'), buildid)
    repo = GitRepository(tmpdir)

    try:
        pbuilderrc = conf('buildbot.environments.%s' % environment)
    except KeyError:
        buildlog(buildid, 'No pbuilderrc defined for environment %s, using stable' % environment)
        pbuilderrc = conf('buildbot.environments.stable')

    output, retcode = repo.clone(gitpath)
    if retcode:
        buildlog(buildid, 'Unable to clone %s. %s\n' % (gitpath, '\n'.join(output)))
        return

    output, retcode = repo.checkout(ref)
    if retcode:
        buildlog(buildid, 'Unable to checkout %s. %s\n' % (ref, '\n'.join(output)))
        return

    if submodules:
        output, retcode = repo.submodule_init()
        buildlog(buildid, output[0])
        buildlog(buildid, output[1])
        output, retcode = repo.submodule_update()
        buildlog(buildid, output[0])
        buildlog(buildid, output[1])

    resultsdir = os.path.join(tmpdir, '.build_results')
    os.makedirs(resultsdir)

    outputfile = os.path.join(conf('buildbot.buildpath'), '%s/build.log' % buildid)
    retcode = repo.build(conf('buildbot.signkey'), pbuilderrc, resultsdir, outputfile)

    logging.debug('build returned %i' % retcode)
    #logging.debug(output[0])
    #logging.debug(output[1])

    os.chdir(resultsdir)
    if not os.listdir(resultsdir) or retcode != 0:
        buildlog(buildid, 'Nothing in results directory. Giving up.')
        return

    tarpath = os.path.join(tmpdir, 'package.tar.gz')
    tar = tarfile.open(tarpath, 'w:gz')
    for name in os.listdir(resultsdir):
        tar.add(name)
    tar.close()

    buildlog(buildid, 'Build complete. Results in %s\n' % tarpath)
    data = file(tarpath, 'rb').read()
    buildlog(buildid, 'Built %i byte tarball' % len(data))

    if cburl:
        for url in cburl.split(','):
            if not url.startswith('http://'):
                url = urljoin('http://127.0.0.1/', url)

            try:
                buildlog(buildid, 'Performing callback: %s' % url)
                req = Curl()
                req.setopt(req.POST, 1)
                req.setopt(req.URL, url)
                req.setopt(req.HTTPPOST, [('package', (req.FORM_FILE, str(tarpath)))])
                req.setopt(req.WRITEDATA, file('%s/build.log' % tmpdir, 'a+'))
                req.perform()
                req.close()
            except:
                buildlog(buildid, 'Callback to %s failed: %s' % (url, format_exc()))
                continue


def build_worker(gitpath, ref, buildid, cburl, submodules):
    if os.fork() == 0:
        build_thread(gitpath, ref, buildid, cburl, submodules)
