#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


import binascii
import os
import os.path
import pathlib
import platform
import shutil
import subprocess
import textwrap

import distutils
from distutils import version
from distutils import extension as distutils_extension
from distutils.command import build as distutils_build
from distutils.command import build_ext as distutils_build_ext

import setuptools
from setuptools.command import develop as setuptools_develop

try:
    import setuptools_rust
except ImportError:
    setuptools_rust = None


RUNTIME_DEPS = [
    'asyncpg~=0.20.0',
    'click~=6.7',
    'httptools>=0.0.13',
    'immutables>=0.13',
    'parsing~=1.6.1',
    'prompt_toolkit>=2.0.0',
    'psutil~=5.6.1',
    'Pygments~=2.3.0',
    'setproctitle~=1.1.10',
    'setuptools-rust==0.10.3',
    'setuptools_scm~=3.2.0',
    'typing_inspect~=0.5.0',
    'uvloop~=0.14.0',
    'wcwidth~=0.1.8',

    'graphql-core~=3.0.3',
    'promise~=2.2.0',

    'edgedb>=0.8.0a1',
]

CYTHON_DEPENDENCY = 'Cython==0.29.14'

DOCS_DEPS = [
    'Sphinx~=2.3.1',
    'lxml~=4.5.1',
    'sphinxcontrib-asyncio~=0.2.0',
]

BUILD_DEPS = [
    CYTHON_DEPENDENCY,
]

RUST_VERSION = '1.42.0'  # Also update docs/internal/dev.rst

EDGEDBCLI_REPO = 'https://github.com/edgedb/edgedb-cli'

EXTRA_DEPS = {
    'test': [
        # Depend on unreleased version for Python 3.8 support,
        'pycodestyle~=2.6.0',
        'pyflakes~=2.2.0',
        'black~=19.3b0',
        'flake8~=3.8.1',
        'flake8-bugbear~=19.8.0',
        'mypy==0.770',
        'coverage~=4.5.2',
        'requests-xml~=0.2.3',
        'lxml',
    ] + DOCS_DEPS,

    'docs': DOCS_DEPS,
}

EXT_CFLAGS = ['-O2']
EXT_LDFLAGS = []

ROOT_PATH = pathlib.Path(__file__).parent.resolve()


if platform.uname().system != 'Windows':
    EXT_CFLAGS.extend([
        '-std=c99', '-fsigned-char', '-Wall', '-Wsign-compare', '-Wconversion'
    ])


def _compile_parsers(build_lib, inplace=False):
    import parsing

    import edb.edgeql.parser.grammar.single as edgeql_spec
    import edb.edgeql.parser.grammar.block as edgeql_spec2
    import edb.edgeql.parser.grammar.sdldocument as schema_spec

    for spec in (edgeql_spec, edgeql_spec2, schema_spec):
        spec_path = pathlib.Path(spec.__file__).parent
        subpath = pathlib.Path(str(spec_path)[len(str(ROOT_PATH)) + 1:])
        pickle_name = spec.__name__.rpartition('.')[2] + '.pickle'
        pickle_path = subpath / pickle_name
        cache = build_lib / pickle_path
        cache.parent.mkdir(parents=True, exist_ok=True)
        parsing.Spec(spec, pickleFile=str(cache), verbose=True)
        if inplace:
            shutil.copy2(cache, ROOT_PATH / pickle_path)


def _compile_build_meta(build_lib, version, pg_config, runstatedir,
                        shared_dir, version_suffix):
    import pkg_resources
    from edb.server import buildmeta

    parsed_version = buildmeta.parse_version(
        pkg_resources.parse_version(version))

    vertuple = list(parsed_version._asdict().values())
    vertuple[2] = int(vertuple[2])
    if version_suffix:
        vertuple[4] = tuple(version_suffix.split('.'))
    vertuple = tuple(vertuple)

    content = textwrap.dedent('''\
        #
        # This source file is part of the EdgeDB open source project.
        #
        # Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
        #
        # Licensed under the Apache License, Version 2.0 (the "License");
        #
        # THIS FILE HAS BEEN AUTOMATICALLY GENERATED.
        #

        PG_CONFIG_PATH = {pg_config!r}
        RUNSTATE_DIR = {runstatedir!r}
        SHARED_DATA_DIR = {shared_dir!r}
        VERSION = {version!r}
    ''').format(
        version=vertuple,
        pg_config=pg_config,
        runstatedir=runstatedir,
        shared_dir=shared_dir,
    )

    directory = build_lib / 'edb' / 'server'
    if not directory.exists():
        directory.mkdir(parents=True)

    with open(directory / '_buildmeta.py', 'w+t') as f:
        f.write(content)


def _compile_postgres(build_base, *,
                      force_build=False, fresh_build=True,
                      run_configure=True, build_contrib=True):

    proc = subprocess.run(
        ['git', 'submodule', 'status', 'postgres'],
        stdout=subprocess.PIPE, universal_newlines=True, check=True)
    status = proc.stdout
    if status[0] == '-':
        print(
            'postgres submodule not initialized, '
            'run `git submodule init; git submodule update`')
        exit(1)

    proc = subprocess.run(
        ['git', 'submodule', 'status', 'postgres'],
        stdout=subprocess.PIPE, universal_newlines=True, check=True)
    revision, _, _ = proc.stdout[1:].partition(' ')
    source_stamp = proc.stdout[0] + revision

    postgres_build = (build_base / 'postgres').resolve()
    postgres_src = ROOT_PATH / 'postgres'
    postgres_build_stamp = postgres_build / 'stamp'

    if postgres_build_stamp.exists():
        with open(postgres_build_stamp, 'r') as f:
            build_stamp = f.read()
    else:
        build_stamp = None

    is_outdated = source_stamp != build_stamp

    if is_outdated or force_build:
        system = platform.system()
        if system == 'Darwin':
            uuidlib = 'e2fs'
        elif system == 'Linux':
            uuidlib = 'e2fs'
        else:
            raise NotImplementedError('unsupported system: {}'.format(system))

        if fresh_build and postgres_build.exists():
            shutil.rmtree(postgres_build)
        build_dir = postgres_build / 'build'
        if not build_dir.exists():
            build_dir.mkdir(parents=True)

        if run_configure or fresh_build or is_outdated:
            subprocess.run([
                str(postgres_src / 'configure'),
                '--prefix=' + str(postgres_build / 'install'),
                '--with-uuid=' + uuidlib,
            ], check=True, cwd=str(build_dir))

        subprocess.run(
            ['make', 'MAKELEVEL=0', '-j', str(max(os.cpu_count() - 1, 1))],
            cwd=str(build_dir), check=True)

        if build_contrib or fresh_build or is_outdated:
            subprocess.run(
                [
                    'make', '-C', 'contrib', 'MAKELEVEL=0', '-j',
                    str(max(os.cpu_count() - 1, 1))
                ],
                cwd=str(build_dir), check=True)

        subprocess.run(
            ['make', 'MAKELEVEL=0', 'install'],
            cwd=str(build_dir), check=True)

        if build_contrib or fresh_build or is_outdated:
            subprocess.run(
                ['make', '-C', 'contrib', 'MAKELEVEL=0', 'install'],
                cwd=str(build_dir), check=True)

        with open(postgres_build_stamp, 'w') as f:
            f.write(source_stamp)


def _check_rust():
    try:
        ver = subprocess.check_output(["rustc", '-V']).split()[1]
        ver = version.LooseVersion(ver.decode())
        if ver < version.LooseVersion(RUST_VERSION):
            raise RuntimeError(
                f'please upgrade Rust to {RUST_VERSION} to compile '
                f'edgedb from source')
    except FileNotFoundError:
        raise RuntimeError(
            f'please install rustc >= {RUST_VERSION} to compile '
            f'edgedb from source (see https://rustup.rs/)')


class build(distutils_build.build):

    user_options = distutils_build.build.user_options + [
        ('pg-config=', None, 'path to pg_config to use with this build'),
        ('runstatedir=', None, 'directory to use for the runtime state'),
        ('shared-dir=', None, 'directory to use for shared data'),
        ('version-suffix=', None, 'dot-separated local version suffix'),
    ]

    def initialize_options(self):
        super().initialize_options()
        self.pg_config = None
        self.runstatedir = None
        self.shared_dir = None
        self.version_suffix = None

    def finalize_options(self):
        super().finalize_options()

    def run(self, *args, **kwargs):
        super().run(*args, **kwargs)
        build_lib = pathlib.Path(self.build_lib)
        _compile_parsers(build_lib)
        if self.pg_config:
            _compile_build_meta(
                build_lib,
                self.distribution.metadata.version,
                self.pg_config,
                self.runstatedir,
                self.shared_dir,
                self.version_suffix,
            )


class develop(setuptools_develop.develop):

    def run(self, *args, **kwargs):
        _check_rust()
        build = self.get_finalized_command('build')
        rust_tmp = pathlib.Path(build.build_temp) / 'rust' / 'cli'
        build_base = pathlib.Path(build.build_base).resolve()
        rust_root = build_base / 'cli'
        env = dict(os.environ)
        env['CARGO_TARGET_DIR'] = str(rust_tmp)
        env['PSQL_DEFAULT_PATH'] = build_base / 'postgres' / 'install' / 'bin'

        subprocess.run(
            [
                'cargo', 'install',
                '--verbose', '--verbose',
                '--git', EDGEDBCLI_REPO,
                '--bin', 'edgedb',
                '--root', rust_root,
                '--features=dev_mode',
                '--locked',
                '--debug',
            ],
            env=env,
            check=True,
        )

        shutil.copy(
            rust_root / 'bin' / 'edgedb',
            ROOT_PATH / 'edb' / 'cli' / 'edgedb',
        )

        scripts = self.distribution.entry_points['console_scripts']
        patched_scripts = []
        for s in scripts:
            if 'rustcli' not in s:
                s = f'{s}_dev'
            patched_scripts.append(s)
        patched_scripts.append('edb = edb.tools.edb:edbcommands')
        self.distribution.entry_points['console_scripts'] = patched_scripts

        super().run(*args, **kwargs)

        _compile_parsers(build_base / 'lib', inplace=True)
        _compile_postgres(build_base)


class gen_build_cache_key(setuptools.Command):

    description = "generate a hash of build dependencies"
    user_options = []

    def run(self, *args, **kwargs):
        import edb as _edb
        from edb.server.buildmeta import hash_dirs

        parser_hash = hash_dirs([(
            os.path.join(_edb.__path__[0], 'edgeql/parser/grammar'),
            '.py')])

        proc = subprocess.run(
            ['git', 'submodule', 'status', 'postgres'],
            stdout=subprocess.PIPE, universal_newlines=True, check=True)
        postgres_revision, _, _ = proc.stdout[1:].partition(' ')

        print(f'{binascii.hexlify(parser_hash).decode()}-{postgres_revision}')

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass


class build_postgres(setuptools.Command):

    description = "build postgres"

    user_options = [
        ('configure', None, 'run ./configure'),
        ('build-contrib', None, 'build contrib'),
        ('fresh-build', None, 'rebuild from scratch'),
    ]

    def initialize_options(self):
        self.configure = False
        self.build_contrib = False
        self.fresh_build = False

    def finalize_options(self):
        pass

    def run(self, *args, **kwargs):
        build = self.get_finalized_command('build')
        _compile_postgres(
            pathlib.Path(build.build_base).resolve(),
            force_build=True,
            fresh_build=self.fresh_build,
            run_configure=self.configure,
            build_contrib=self.build_contrib)


class build_ext(distutils_build_ext.build_ext):

    user_options = distutils_build_ext.build_ext.user_options + [
        ('cython-annotate', None,
            'Produce a colorized HTML version of the Cython source.'),
        ('cython-directives=', None,
            'Cython compiler directives'),
    ]

    def initialize_options(self):
        # initialize_options() may be called multiple times on the
        # same command object, so make sure not to override previously
        # set options.
        if getattr(self, '_initialized', False):
            return

        super(build_ext, self).initialize_options()

        if os.environ.get('EDGEDB_DEBUG'):
            self.cython_always = True
            self.cython_annotate = True
            self.cython_directives = "linetrace=True"
            self.define = 'PG_DEBUG,CYTHON_TRACE,CYTHON_TRACE_NOGIL'
            self.debug = True
        else:
            self.cython_always = False
            self.cython_annotate = None
            self.cython_directives = None
            self.debug = False

    def finalize_options(self):
        # finalize_options() may be called multiple times on the
        # same command object, so make sure not to override previously
        # set options.
        if getattr(self, '_initialized', False):
            return

        import pkg_resources

        # Double check Cython presence in case setup_requires
        # didn't go into effect (most likely because someone
        # imported Cython before setup_requires injected the
        # correct egg into sys.path.
        try:
            import Cython
        except ImportError:
            raise RuntimeError(
                'please install {} to compile edgedb from source'.format(
                    CYTHON_DEPENDENCY))

        cython_dep = pkg_resources.Requirement.parse(CYTHON_DEPENDENCY)
        if Cython.__version__ not in cython_dep:
            raise RuntimeError(
                'edgedb requires {}, got Cython=={}'.format(
                    CYTHON_DEPENDENCY, Cython.__version__
                ))

        from Cython.Build import cythonize

        directives = {
            'language_level': '3'
        }

        if self.cython_directives:
            for directive in self.cython_directives.split(','):
                k, _, v = directive.partition('=')
                if v.lower() == 'false':
                    v = False
                if v.lower() == 'true':
                    v = True

                directives[k] = v

        self.distribution.ext_modules[:] = cythonize(
            self.distribution.ext_modules,
            compiler_directives=directives,
            annotate=self.cython_annotate,
            include_path=["edb/server/pgproto/"])

        super(build_ext, self).finalize_options()

    def run(self):
        if self.distribution.rust_extensions:
            distutils.log.info("running build_rust")
            _check_rust()
            build_rust = self.get_finalized_command("build_rust")
            build_ext = self.get_finalized_command("build_ext")
            copy_list = []
            if not self.inplace:
                for ext in self.distribution.rust_extensions:
                    # Always build in-place because later stages of the build
                    # may depend on the modules having been built
                    dylib_path = pathlib.Path(
                        build_ext.get_ext_fullpath(ext.name))
                    build_ext.inplace = True
                    target_path = pathlib.Path(
                        build_ext.get_ext_fullpath(ext.name))
                    build_ext.inplace = False
                    copy_list.append((dylib_path, target_path))

                    # Workaround a bug in setuptools-rust: it uses
                    # shutil.copyfile(), which is not safe w.r.t mmap,
                    # so if the target module has been previously loaded
                    # bad things will happen.
                    if target_path.exists():
                        target_path.unlink()

                    target_path.parent.mkdir(parents=True, exist_ok=True)

            build_rust.debug = self.debug
            os.environ['CARGO_TARGET_DIR'] = (
                str(pathlib.Path(self.build_temp) / 'rust' / 'extensions'))
            build_rust.run()

            for src, dst in copy_list:
                shutil.copyfile(src, dst)

        super().run()


if setuptools_rust is not None:
    rust_extensions = [
        setuptools_rust.RustExtension(
            "edb._edgeql_rust",
            path="edb/edgeql-rust/Cargo.toml",
            binding=setuptools_rust.Binding.RustCPython,
        ),
        setuptools_rust.RustExtension(
            "edb._graphql_rewrite",
            path="edb/graphql-rewrite/Cargo.toml",
            binding=setuptools_rust.Binding.RustCPython,
        ),
    ]
else:
    rust_extensions = []


setuptools.setup(
    setup_requires=RUNTIME_DEPS + BUILD_DEPS,
    use_scm_version=True,
    name='edgedb-server',
    description='EdgeDB Server',
    author='MagicStack Inc.',
    author_email='hello@magic.io',
    packages=['edb'],
    include_package_data=True,
    cmdclass={
        'build': build,
        'build_ext': build_ext,
        'develop': develop,
        'build_postgres': build_postgres,
        'gen_build_cache_key': gen_build_cache_key,
    },
    entry_points={
        'console_scripts': [
            'edgedb-old = edb.cli:cli',
            'edgedb-server = edb.server.main:main',
            'edgedb = edb.cli:rustcli',
        ]
    },
    ext_modules=[
        distutils_extension.Extension(
            "edb.testbase.protocol.protocol",
            ["edb/testbase/protocol/protocol.pyx"],
            extra_compile_args=EXT_CFLAGS,
            extra_link_args=EXT_LDFLAGS),

        distutils_extension.Extension(
            "edb.server.pgproto.pgproto",
            ["edb/server/pgproto/pgproto.pyx"],
            extra_compile_args=EXT_CFLAGS,
            extra_link_args=EXT_LDFLAGS),

        distutils_extension.Extension(
            "edb.server.dbview.dbview",
            ["edb/server/dbview/dbview.pyx"],
            extra_compile_args=EXT_CFLAGS,
            extra_link_args=EXT_LDFLAGS),

        distutils_extension.Extension(
            "edb.server.tokenizer",
            ["edb/server/tokenizer.pyx"],
            extra_compile_args=EXT_CFLAGS,
            extra_link_args=EXT_LDFLAGS),

        distutils_extension.Extension(
            "edb.server.mng_port.edgecon",
            ["edb/server/mng_port/edgecon.pyx"],
            extra_compile_args=EXT_CFLAGS,
            extra_link_args=EXT_LDFLAGS),

        distutils_extension.Extension(
            "edb.server.cache.stmt_cache",
            ["edb/server/cache/stmt_cache.pyx"],
            extra_compile_args=EXT_CFLAGS,
            extra_link_args=EXT_LDFLAGS),

        distutils_extension.Extension(
            "edb.server.pgcon.pgcon",
            ["edb/server/pgcon/pgcon.pyx"],
            extra_compile_args=EXT_CFLAGS,
            extra_link_args=EXT_LDFLAGS),

        distutils_extension.Extension(
            "edb.server.http.http",
            ["edb/server/http/http.pyx"],
            extra_compile_args=EXT_CFLAGS,
            extra_link_args=EXT_LDFLAGS),

        distutils_extension.Extension(
            "edb.server.http_edgeql_port.protocol",
            ["edb/server/http_edgeql_port/protocol.pyx"],
            extra_compile_args=EXT_CFLAGS,
            extra_link_args=EXT_LDFLAGS),

        distutils_extension.Extension(
            "edb.server.http_graphql_port.protocol",
            ["edb/server/http_graphql_port/protocol.pyx"],
            extra_compile_args=EXT_CFLAGS,
            extra_link_args=EXT_LDFLAGS),

        distutils_extension.Extension(
            "edb.server.notebook_port.protocol",
            ["edb/server/notebook_port/protocol.pyx"],
            extra_compile_args=EXT_CFLAGS,
            extra_link_args=EXT_LDFLAGS),
    ],
    rust_extensions=rust_extensions,
    install_requires=RUNTIME_DEPS,
    extras_require=EXTRA_DEPS,
    test_suite='tests.suite',
)
