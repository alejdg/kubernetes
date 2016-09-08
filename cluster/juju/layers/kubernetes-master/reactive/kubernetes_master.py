import os

from shlex import split
from subprocess import call
from subprocess import check_call
from subprocess import check_output

from charms.reactive import remove_state
from charms.reactive import set_state
from charms.reactive import when
from charms.reactive import when_not

from charmhelpers.core import hookenv
from charmhelpers.core import unitdata
from charmhelpers.core.templating import render


@when_not('kube_master_components.installed')
def install():
    '''Unpack the Kubernetes master binary files.'''
    files_dir = os.path.join(hookenv.charm_dir(), 'files')
    archive = os.path.join(files_dir, 'kubernetes.tar.gz')
    command = 'tar -xvzf {0} -C {1}'.format(archive, files_dir)
    return_code = call(split(command))
    dest_dir = '/usr/local/bin/'
    services = ['kube-apiserver',
                'kube-controller-manager',
                'kube-scheduler',
                'kube-dns']
    for service in services:
        install = 'install {0}/{1} {3}'.format(files_dir, service, dest_dir)
        return_code = call(split(install))
        if return_code != 0:
            raise Exception('Unable to install {0}'.format(service))


@when('k8s.certificate.authority available')
@when('etcd.available')
def start_master(etcd):
    '''Run the Kubernetes master components.'''
    hookenv.status_set('maintenance',
                       'Rendering the Kubernetes master systemd files.')
    # Use the etcd relation object to render files with etcd information.
    render_files(etcd)
    hookenv.status_set('maintenance',
                       'Starting the Kubernetes master services.')
    services = ['kube-apiserver',
                'kube-controller-manager',
                'kube-scheduler']
    for service in services:
        if start_service(service):
            set_state('{0}.available'.format(service))


@when('apiserver.available')
@when_not('kube-dns.available')
def launch_dns():
    '''Create the "kube-system" namespace, the kubedns resource controller, and
    the kubedns service. '''
    hookenv.status_set('maintenance',
                       'Rendering the Kubernetes DNS systemd files.')
    # Run a command to check if the apiserver is responding.
    return_code = call(split('kubectl cluster-info'))
    if return_code != 0:
        hookenv.log('kubectl command failed, waiting for apiserver to start.')
        remove_state('kubedns.available')
        # Return without setting kubedns.available so this method will retry.
        return
    # Check for the "kube-system" namespace.
    return_code = call(split('kubectl get namespace kube-system'))
    if return_code != 0:
        # Create the kube-system namespace that is used by the kubedns files.
        check_call(split('kubectl create namespace kube-system'))
    # Rember
    render_files()
    if start_service('kube-dns'):
        set_state('kube-dns.available')


def arch():
    '''Return the package architecture as a string. Raise an exception if the
    architecture is not supported by kubernetes.'''
    # Get the package architecture for this system.
    architecture = check_output(['dpkg', '--print-architecture']).rstrip()
    # Convert the binary result into a string.
    architecture = architecture.decode('utf-8')
    # Validate the architecture is supported by kubernetes.
    if architecture not in ['amd64', 'arm', 'arm64', 'ppc64le']:
        message = 'Unsupported machine architecture: {0}'.format(architecture)
        hookenv.status_set('blocked', message)
        raise Exception(message)
    return architecture


def render_files(reldata=None):
    '''Use jinja templating to render the docker-compose.yml and master.json
    file to contain the dynamic data for the configuration files.'''
    context = {}
    # Load the context data with SDN data.
    context.update(gather_sdn_data())
    # Add the charm configuration data to the context.
    context.update(hookenv.config())
    # Add the relation data when it is not empty.
    if reldata:
        connection_string = reldata.get_connection_string()
        # Define where the etcd tls files will be kept.
        etcd_dir = '/etc/ssl/etcd'
        # Create paths to the etcd client ca, key, and cert file locations.
        ca = os.path.join(etcd_dir, 'client-ca.pem')
        key = os.path.join(etcd_dir, 'client-key.pem')
        cert = os.path.join(etcd_dir, 'client-cert.pem')
        # Save the client credentials (in relation data) to the paths provided.
        reldata.save_client_credentials(key, cert, ca)
        # Update the context so the template has the etcd information.
        context.update({'etcd_dir': etcd_dir,
                        'connection_string': connection_string,
                        'etcd_ca': ca,
                        'etcd_key': key,
                        'etcd_cert': cert})

    charm_dir = hookenv.charm_dir()
    rendered_kube_dir = os.path.join(charm_dir, 'files/kubernetes')
    if not os.path.exists(rendered_kube_dir):
        os.makedirs(rendered_kube_dir)
    rendered_manifest_dir = os.path.join(charm_dir, 'files/manifests')
    if not os.path.exists(rendered_manifest_dir):
        os.makedirs(rendered_manifest_dir)

    # Update the context with extra values, arch, manifest dir, and private IP.
    context.update({'arch': arch(),
                    'master_address': hookenv.unit_get('private-address'),
                    'manifest_directory': rendered_manifest_dir,
                    'public_address': hookenv.unit_get('public-address'),
                    'private_address': hookenv.unit_get('private-address')})

    # Render the configuration files that contains parameters for
    # the apiserver, scheduler, and controller-manager
    render_service('kube-apiserver', context)
    render_service('kube-controller-manager', context)
    render_service('kube-scheduler', context)
    render_service('kube-dns', context)


def gather_sdn_data():
    '''Get the Software Defined Network (SDN) information and return it as a
    dictionary. '''
    sdn_data = {}
    # The dictionary named 'pillar' is a construct of the k8s template files.
    pillar = {}
    # SDN Providers pass data via the unitdata.kv module
    db = unitdata.kv()
    # Ideally the DNS address should come from the sdn cidr.
    subnet = db.get('sdn_subnet')
    if subnet:
        # Generate the DNS ip address on the SDN cidr (this is desired).
        pillar['dns_server'] = get_dns_ip(subnet)
    else:
        # There is no SDN cider fall back to the kubernetes config cidr option.
        pillar['dns_server'] = get_dns_ip(hookenv.config().get('cidr'))
    # The pillar['dns_server'] value is used the kubedns-svc.yaml file.
    pillar['dns_replicas'] = 1
    # The pillar['dns_domain'] value is used in the kubedns-rc.yaml
    pillar['dns_domain'] = hookenv.config().get('dns_domain')
    # Use a 'pillar' dictionary so we can reuse the upstream kubedns templates.
    sdn_data['pillar'] = pillar
    return sdn_data


def get_dns_ip(cidr):
    '''Get an IP address for the DNS server on the provided cidr.'''
    # Remove the range from the cidr.
    ip = cidr.split('/')[0]
    # Take the last octet off the IP address and replace it with 10.
    return '.'.join(ip.split('.')[0:-1]) + '.10'


def get_sdn_ip(cidr):
    '''Get the IP address for the SDN gateway based on the provided cidr.'''
    # Remove the range from the cidr.
    ip = cidr.split('/')[0]
    # Remove the last octet and replace it with 1.
    return '.'.join(ip.split('.')[0:-1]) + '.1'


def start_service(service_name):
    '''Start the systemd service by name return True if the command was
    successful.'''
    start = 'systemctl start {0}'.format(service_name)
    print(start)
    return_code = call(split(start))
    return return_code == 0


def render_service(service_name, context):
    '''Render the systemd service by name.'''
    unit_directory = '/etc/systemd/system'
    source = '{0}.service'.format(service_name)
    target = os.path.join(unit_directory, service_name)
    render(source, target, context)
    conf_directory = '/etc/defaults/{0}'.format(service_name)
    source = '{0}.defaults'.format(service_name)
    target = os.path.join(conf_directory, service_name)
    render(source, target, context)
