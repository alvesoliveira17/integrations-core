# (C) Datadog, Inc. 2018-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import functools
import time
import typing

import click
import semver

from ....subprocess import run_command
from ....utils import basepath, chdir, get_next
from ...constants import CHANGELOG_LABEL_PREFIX, CHANGELOG_TYPE_NONE, get_root
from ...github import get_pr, get_pr_from_hash, get_pr_labels, get_pr_milestone, parse_pr_number
from ...trello import TrelloClient
from ...utils import format_commit_id
from ..console import CONTEXT_SETTINGS, abort, echo_failure, echo_info, echo_success, echo_waiting, echo_warning


def validate_version(ctx: click.Context, param: click.Parameter, value: str, ignore: typing.Sequence[str] = ()) -> str:
    if value in ignore:
        return value

    def parse_version(version: str) -> semver.VersionInfo:
        try:
            return semver.parse_version_info(version)
        except ValueError:
            raise click.BadParameter('needs to be in semver format M.m[.p[-r]]')

    parts = value.split('.')

    if len(parts) == 2:
        example_release_tag = f'{value}.1'
        example_release_branch = f'{value}.x'
        raise click.BadParameter(
            f"Using a minor version ({value!r}) is ambiguous. Use a release tag (e.g. {example_release_tag!r}) "
            f"or a release branch (e.g. {example_release_branch!r}) instead."
        )

    if len(parts) == 3 and parts[-1] == 'x':
        # Treat as a release branch, e.g. '7.17.x'.
        parts[2] = '0'
        version_info = parse_version('.'.join(parts))
        return f'{version_info.major}.{version_info.minor}.x'

    # Treat as a fully-resolved, semver-compliant version string, e.g. '7.17.0', or '7.17.0-rc.2'.
    version_info = parse_version('.'.join(parts))
    return str(version_info)


def create_trello_card(client, teams, pr_title, pr_url, pr_body, dry_run):
    body = f'Pull request: {pr_url}\n\n{pr_body}'

    for team in teams:
        if dry_run:
            echo_success(f'Will create a card for team {team}: ', nl=False)
            echo_info(pr_title)
            continue
        creation_attempts = 3
        for attempt in range(3):
            rate_limited, error, response = client.create_card(team, pr_title, body)
            if rate_limited:
                wait_time = 10
                echo_warning(
                    'Attempt {} of {}: A rate limit in effect, retrying in {} '
                    'seconds...'.format(attempt + 1, creation_attempts, wait_time)
                )
                time.sleep(wait_time)
            elif error:
                if attempt + 1 == creation_attempts:
                    echo_failure(f'Error: {error}')
                    break

                wait_time = 2
                echo_warning(
                    'Attempt {} of {}: An error has occurred, retrying in {} '
                    'seconds...'.format(attempt + 1, creation_attempts, wait_time)
                )
                time.sleep(wait_time)
            else:
                echo_success(f'Created card for team {team}: ', nl=False)
                echo_info(response.json().get('url'))
                break


@click.command(
    context_settings=CONTEXT_SETTINGS, short_help='Create a Trello card for each change that needs to be tested'
)
@click.argument('base_ref', callback=validate_version)
@click.argument(
    'target_ref', callback=functools.partial(validate_version, ignore=['origin/master']), default='origin/master'
)
@click.option('--milestone', help='The PR milestone to filter by')
@click.option('--dry-run', '-n', is_flag=True, help='Only show the changes')
@click.pass_context
def testable(ctx: click.Context, base_ref: str, target_ref: str, milestone: str, dry_run: bool) -> None:
    """
    Create a Trello card for changes since a previous release (referenced by BASE_REF)
    that need to be tested for the next release (referenced by TARGET_REF).

    BASE_REF and TARGET_REF can refer to:

    * A git tag: 6.17.1, 7.17.0-rc.4, ...\n
    * A release branch: 6.16.x, 7.17.x, ...

    If not specified, TARGET_REF defaults to `origin/master`.

    NOTE: using a minor version shorthand, such as '7.16', is not supported anymore, as it is ambiguous whether you'd
    refer to the latest patch release (e.g. 7.16.1) or the release branch (e.g. 7.16.x).

    Example: assuming we are working on the release of 7.17.0, we can...

    * Create cards for changes between a previous Agent release and `master` (useful when preparing an initial RC):\n
        $ ddev release testable 7.16.1

    * Create cards for changes between a previous RC and `master` (useful when preparing a new RC, and a separate
    release branch was not created yet):\n
        $ ddev release testable 7.17.0-rc.2

    * Create cards for changes between a previous RC and a release branch (useful to only review changes in a
    release branch that has diverged from `master`):\n
        $ ddev release testable 7.17.0-rc.4 7.17.x

    * Create cards for changes between two arbitrary tags, e.g. between RCs:\n
        $ ddev release testable 7.17.0-rc.4 7.17.0-rc.5

    TIP: run with `ddev -x release testable` to force the use of the current directory.

    To avoid GitHub's public API rate limits, you need to set
    `github.user`/`github.token` in your config file or use the
    `DD_GITHUB_USER`/`DD_GITHUB_TOKEN` environment variables.

    \b
    To use Trello:
    1. Go to `https://trello.com/app-key` and copy your API key.
    2. Run `ddev config set trello.key` and paste your API key.
    3. Go to `https://trello.com/1/authorize?key=key&name=name&scope=read,write&expiration=never&response_type=token`,
       where `key` is your API key and `name` is the name to give your token, e.g. ReleaseTestingYourName.
       Authorize access and copy your token.
    4. Run `ddev config set trello.token` and paste your token.
    """
    root = get_root()
    repo = basepath(root)
    if repo not in ('integrations-core', 'datadog-agent'):
        abort(f'Repo `{repo}` is unsupported.')

    echo_info(f'Ref {base_ref!r} will be compared to {target_ref!r}.')

    echo_waiting('Getting diff... ', nl=False)
    diff_command = 'git --no-pager log "--pretty=format:%H %s" {}..{}'

    with chdir(root):
        fetch_command = 'git fetch --dry'
        result = run_command(fetch_command, capture=True)
        if result.code:
            abort(f'Unable to run {fetch_command}.')

        if base_ref in result.stderr or target_ref in result.stderr:
            abort(f'Your repository is not sync with the remote repository. Please run git fetch in {root!r} folder.')

        reftag = f"refs/tags/{base_ref}"
        result = run_command(diff_command.format(reftag, target_ref), capture=True)
        if result.code:
            origin_release_branch = f'origin/{base_ref}'
            echo_failure('failed!')
            echo_waiting(
                f'Tag {base_ref!r} does not exist, retrying with release branch {origin_release_branch!r}...'
            )
            result = run_command(diff_command.format(origin_release_branch, target_ref), capture=True)
            if result.code:
                abort(
                    'Unable to get the diff. '
                    f'HINT: ensure {base_ref!r} and {target_ref!r} both refer to either an existing tag, '
                    'or a release branch.'
                )
            else:
                echo_success('success!')
        else:
            echo_success('success!')

    # [(commit_hash, commit_subject), ...]
    diff_data = [tuple(line.split(None, 1)) for line in reversed(result.stdout.splitlines())]
    num_changes = len(diff_data)

    if repo == 'integrations-core':
        options = {'1': 'Integrations', '2': 'Containers', '3': 'Core', '4': 'Platform', 's': 'Skip', 'q': 'Quit'}
    else:
        options = {
            '1': 'Core',
            '2': 'Containers',
            '3': 'Logs',
            '4': 'Platform',
            '5': 'Process',
            '6': 'Trace',
            '7': 'Integrations',
            's': 'Skip',
            'q': 'Quit',
        }
    default_option = get_next(options)
    options_prompt = f'Choose an option (default {options[default_option]}): '
    options_text = '\n' + '\n'.join('{} - {}'.format(key, value) for key, value in options.items())

    commit_ids: typing.Set[str] = set()
    user_config = ctx.obj
    trello = TrelloClient(user_config)

    for i, (commit_hash, commit_subject) in enumerate(diff_data, 1):
        commit_id = parse_pr_number(commit_subject)
        if commit_id:
            api_response = get_pr(commit_id, user_config, raw=True)
            if api_response.status_code == 401:
                abort('Access denied. Please ensure your GitHub token has correct permissions.')
            elif api_response.status_code == 403:
                echo_failure(
                    'Error getting info for #{}. Please set a GitHub HTTPS '
                    'token to avoid rate limits.'.format(commit_id)
                )
                continue
            elif api_response.status_code == 404:
                echo_info(f'Skipping #{commit_id}, not a pull request...')
                continue

            api_response.raise_for_status()
            pr_data = api_response.json()
        else:
            try:
                api_response = get_pr_from_hash(commit_hash, repo, user_config, raw=True)
                if api_response.status_code == 401:
                    abort('Access denied. Please ensure your GitHub token has correct permissions.')
                elif api_response.status_code == 403:
                    echo_failure(
                        'Error getting info for #{}. Please set a GitHub HTTPS '
                        'token to avoid rate limits.'.format(commit_hash)
                    )
                    continue

                api_response.raise_for_status()
                pr_data = api_response.json()
                pr_data = pr_data.get('items', [{}])[0]
            # Commit to master
            except IndexError:
                pr_data = {
                    'number': commit_hash,
                    'html_url': f'https://github.com/DataDog/{repo}/commit/{commit_hash}',
                }
            commit_id = str(pr_data.get('number', ''))

        if commit_id and commit_id in commit_ids:
            echo_info(f'Already seen PR #{commit_id}, skipping it.')
            continue
        commit_ids.add(commit_id)

        pr_labels = sorted(get_pr_labels(pr_data))
        documentation_pr = False
        nochangelog_pr = True
        for label in pr_labels:
            if label.startswith('documentation'):
                documentation_pr = True

            if label.startswith(CHANGELOG_LABEL_PREFIX) and label.split('/', 1)[1] != CHANGELOG_TYPE_NONE:
                nochangelog_pr = False

        if documentation_pr and nochangelog_pr:
            echo_info(f'Skipping documentation {format_commit_id(commit_id)}.')
            continue

        pr_milestone = get_pr_milestone(pr_data)
        if milestone and pr_milestone != milestone:
            echo_info(f'Looking for milestone {milestone}, skipping {format_commit_id(commit_id)}.')
            continue

        pr_url = pr_data.get('html_url', f'https://github.com/DataDog/{repo}/pull/{commit_id}')
        pr_title = pr_data.get('title', commit_subject)
        pr_author = pr_data.get('user', {}).get('login', '')
        pr_body = pr_data.get('body', '')

        teams = [trello.label_team_map[label] for label in pr_labels if label in trello.label_team_map]
        if teams:
            create_trello_card(trello, teams, pr_title, pr_url, pr_body, dry_run)
            continue

        finished = False
        choice_error = ''
        progress_status = f'({i} of {num_changes}) '
        indent = ' ' * len(progress_status)

        while not finished:
            echo_success(f'\n{progress_status}{pr_title}')

            echo_success('Url: ', nl=False, indent=indent)
            echo_info(pr_url)

            echo_success('Author: ', nl=False, indent=indent)
            echo_info(pr_author)

            echo_success('Labels: ', nl=False, indent=indent)
            echo_info(', '.join(pr_labels))

            if pr_milestone:
                echo_success('Milestone: ', nl=False, indent=indent)
                echo_info(pr_milestone)

            # Ensure Unix lines feeds just in case
            echo_info(pr_body.strip('\r'), indent=indent)

            echo_info(options_text)

            if choice_error:
                echo_warning(choice_error)

            echo_waiting(options_prompt, nl=False)

            # Terminals are odd and sometimes produce an erroneous null byte
            choice = '\x00'
            while choice == '\x00':
                choice = click.getchar().strip()

            if not choice:
                choice = default_option

            if choice not in options:
                echo_info(choice)
                choice_error = f'`{choice}` is not a valid option.'
                continue
            else:
                choice_error = ''

            value = options[choice]
            echo_info(value)

            if value == 'Skip':
                echo_info(f'Skipped {format_commit_id(commit_id)}')
                break
            elif value == 'Quit':
                echo_warning(f'Exited at {format_commit_id(commit_id)}')
                return
            else:
                create_trello_card(trello, [value], pr_title, pr_url, pr_body, dry_run)

            finished = True
