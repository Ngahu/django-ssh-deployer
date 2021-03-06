import datetime
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from paramiko import SSHClient, AutoAddPolicy


class Command(BaseCommand):
    """
    This command will deploy a Django site to a set or servers over SSH using
    paramiko.
    """

    help = "This command will deploy your Django site via SSH as a user. BE CAREFUL!"

    def add_arguments(self, parser):
        parser.add_argument(
            "--instance",
            action="store",
            dest="instance",
            default=None,
            help="""The instance from DEPLOYER_INSTANCES to deploy.""",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            dest="quiet",
            default=False,
            help="""Sets quiet mode with minimal output.""",
        )
        parser.add_argument(
            "--no-confirm",
            action="store_true",
            dest="no_confirm",
            default=False,
            help="""Publishes without confirmation: be careful!""",
        )
        parser.add_argument(
            "--stamp",
            action="store",
            dest="stamp",
            default="{0:%Y-%m-%d-%H-%M-%S}".format(datetime.datetime.now()),
            help="""Overrides the default timestamp used for the deployment.""",
        )

    def command_output(self, stdout, stderr, quiet):
        """
        Dumps the output of the SSH command run via paramiko and
        error, if applicable.

        Output the errors, even in quiet mode.
        """
        if not quiet:
            output = stdout.read().decode("utf-8").strip()
            if len(output):
                print(output)
        else:
            stdout.read()

        err = stderr.read().decode("utf-8")
        if len(err):
            print(err)

    def handle(self, *args, **options):
        """
        Gets the appropriate settings from Django and deploys the repository
        to the target servers for the instance selected.
        """
        # Grab the quiet settings and unique stamp
        quiet = options["quiet"]
        stamp = options["stamp"]
        no_confirm = options["no_confirm"]

        # Check to ensure the require setting is in Django's settings.
        if hasattr(settings, "DEPLOYER_INSTANCES"):
            instances = settings.DEPLOYER_INSTANCES
        else:
            raise CommandError(
                "You have not configured DEPLOYER_INSTANCES in your Django settings."
            )

        # Grab the instance settings if they're properly set
        if options["instance"] in instances:
            instance = instances[options["instance"]]
        else:
            raise CommandError(
                'The instance name you provided ("{instance}") is not configured in your settings DEPLOYER_INSTANCES. Valid instance names are: {instances}'.format(
                    instance=options["instance"],
                    instances=", ".join(list(instances.keys())),
                )
            )

        print(
            "We are about to deploy the instance '{instance}' to the following servers: {servers}.".format(
                instance=options["instance"], servers=", ".join(instance["servers"])
            )
        )
        if no_confirm:
            verify = "yes"
        else:
            verify = input(
                'Are you sure you want to do this (enter "yes" to proceed)? '
            )

        if verify.lower() != "yes":
            print("You did not type 'yes' - aborting.")
            return

        name_branch_stamp = "{name}-{branch}-{stamp}".format(
            name=instance["name"], branch=instance["branch"], stamp=stamp
        )

        install_code_path_stamp = os.path.join(instance["code_path"], name_branch_stamp)

        # On our first iteration, we clone the repository, build the virtual
        # environment, and collect static files; this may take some time.
        for index, server in enumerate(instance["servers"]):
            # Open an SSH connection to the server
            ssh = SSHClient()
            ssh.set_missing_host_key_policy(AutoAddPolicy())
            ssh.connect(server, username=instance["server_user"])

            print("Preparing code and virtualenv on node: {}...".format(server))

            # Make sure the code_path directory exists
            stdin, stdout, stderr = ssh.exec_command(
                """
                mkdir -p {directory}
                """.format(
                    directory=instance["code_path"]
                )
            )
            self.command_output(stdout, stderr, quiet)

            stdin, stdout, stderr = ssh.exec_command(
                """
                cd {code_path}
                git clone --recursive --verbose -b {branch} {repository} {name}-{branch}-{stamp}
                """.format(
                    code_path=instance["code_path"],
                    name=instance["name"],
                    branch=instance["branch"],
                    repository=instance["repository"],
                    stamp=stamp,
                )
            )
            self.command_output(stdout, stderr, quiet)

            print(
                "Installing requirements in a new venv, collecting static files, and running migrations..."
            )

            pip_text = ""
            if "upgrade_pip" in instance and instance["upgrade_pip"]:
                print("pip will be upgraded to the latest version...")
                pip_text = "pip install -U pip"

            stdin, stdout, stderr = ssh.exec_command(
                """
                cd {install_code_path_stamp}
                {venv_python_path} -m venv venv
                . venv/bin/activate
                {pip_text}
                pip install --ignore-installed -r {requirements}
                python manage.py collectstatic --noinput --settings={settings}
                """.format(
                    install_code_path_stamp=install_code_path_stamp,
                    venv_python_path=instance["venv_python_path"],
                    pip_text=pip_text,
                    requirements=instance["requirements"],
                    settings=instance["settings"],
                )
            )
            self.command_output(stdout, stderr, quiet)

            if "selinux" in instance and instance["selinux"]:
                print("Setting security context for RedHat / CentOS SELinux...")

                stdin, stdout, stderr = ssh.exec_command(
                    """
                    chcon -Rv --type=httpd_sys_content_t {install_code_path_stamp} > /dev/null
                    find {install_code_path_stamp}/venv/ \( -name "*.so" -o -name "*.so.*" \) -exec chcon -Rv --type=httpd_sys_script_exec_t {{}} \; > /dev/null
                    """.format(
                        install_code_path_stamp=install_code_path_stamp
                    )
                )
                self.command_output(stdout, stderr, quiet)

        # On our second iteration, we update symlinks, keep only recent deployments,
        # run any additional command, and run migrations. This should be fast.
        for index, server in enumerate(instance["servers"]):
            # Open an SSH connection to the server
            ssh = SSHClient()
            ssh.set_missing_host_key_policy(AutoAddPolicy())
            ssh.connect(server, username=instance["server_user"])

            print("")
            print(
                "Updating symlinks and running any additional defined commands on node: {}...".format(
                    server
                )
            )

            stdin, stdout, stderr = ssh.exec_command(
                """
                ln -sfn {install_code_path_stamp} {install_code_path}
                """.format(
                    install_code_path_stamp=install_code_path_stamp,
                    install_code_path=os.path.join(
                        instance["code_path"],
                        "{name}-{branch}".format(
                            name=instance["name"], branch=instance["branch"]
                        ),
                    ),
                )
            )

            self.command_output(stdout, stderr, quiet)

            if int(instance.get("save_deploys", 0)) > 0:
                print(
                    "Keeping the {} most recent deployments, and deleting the rest on node: {}".format(
                        instance["save_deploys"], server
                    )
                )

                stdin, stdout, stderr = ssh.exec_command(
                    """
                    ls -1trd {path}/{name}-{branch}* | head -n -{save_deploys} | xargs -d '\\n' rm -rf --
                    """.format(
                        path=instance["code_path"],
                        name=instance["name"],
                        branch=instance["branch"],
                        save_deploys=instance["save_deploys"] + 1,
                    )
                )
                self.command_output(stdout, stderr, quiet)

            if "additional_commands" in instance:
                print("Performing defined additional commands...")
                for additional_command in instance["additional_commands"]:
                    print("Running '{}'...".format(additional_command))
                    stdin, stdout, stderr = ssh.exec_command(additional_command)
                    self.command_output(stdout, stderr, quiet)

            # Only run migrations when we're on the last server.
            if index + 1 == len(instance["servers"]):
                print("Finally, running migrations...")
                stdin, stdout, stderr = ssh.exec_command(
                    """
                    cd {install_code_path_stamp}
                    . venv/bin/activate
                    python manage.py migrate --noinput --settings={settings}
                    """.format(
                        install_code_path_stamp=install_code_path_stamp,
                        settings=instance["settings"],
                    )
                )
                self.command_output(stdout, stderr, quiet)

        print("All done!")
