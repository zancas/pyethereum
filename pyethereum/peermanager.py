import time
import socket
import logging

from dispatch import receiver
import netifaces

from common import StoppableLoopThread
import signals
from peer import Peer

logger = logging.getLogger(__name__)


class PeerManager(StoppableLoopThread):

    max_silence = 10  # how long before pinging a peer
    max_ping_wait = 5  # how long to wait before disconenctiong after ping
    max_ask_for_peers_elapsed = 30  # how long before asking for peers

    def __init__(self):
        super(PeerManager, self).__init__()
        self.connected_peers = set()
        self._known_peers = set()  # (ip, port, node_id)
        self.local_addresses = []  # [(ip, port)]

    def configure(self, config):
        self.config = config

    def stop(self):
        with self.lock:
            if not self._stopped:
                for peer in self.connected_peers:
                    peer.stop()
        super(PeerManager, self).stop()

    def add_known_peer_address(self, ip, port, node_id):
        ipn = (ip, port, node_id)
        with self.lock:
            if ipn not in self._known_peers:
                self._known_peers.add(ipn)

    def get_known_peer_addresses(self):
        return set(self._known_peers).union(
            self.get_connected_peer_addresses())

    def get_connected_peer_addresses(self):
        "get peers, we connected and have a port"
        return set((p.ip, p.port, p.node_id) for p in self.connected_peers
                   if p.port)

    def remove_peer(self, peer):
        if not peer.stopped():
            peer.stop()
        with self.lock:
            self.connected_peers.remove(peer)
        # connect new peers if there are no candidates

    def _create_peer_sock(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1)
        return sock

    def connect_peer(self, host, port):
        '''
        :param host:  domain notation or IP
        '''
        sock = self._create_peer_sock()
        logger.debug('connecting {0}:{1}'.format(host, port))
        try:
            sock.connect((host, port))
        except Exception as e:
            logger.debug(
                'Conencting {0}:{1} failed, {2}'.format(host, port, str(e)))
            return None
        ip, port = sock.getpeername()
        logger.debug('connected {0}:{1}'.format(ip, port))
        peer = self.add_peer(sock, ip, port)

        # Send Hello
        peer.send_Hello()
        return peer

    def get_peer_candidates(self):
        candidates = self.get_known_peer_addresses().difference(
            self.get_connected_peer_addresses())
        candidates = [
            ipn for ipn in candidates if ipn[:2] not in self.local_addresses]
        return candidates

    def _check_alive(self, peer):
        if peer.stopped():
            self.remove_peer(peer)
            return

        now = time.time()
        dt_ping = now - peer.last_pinged
        dt_seen = now - peer.last_valid_packet_received

        # if ping was sent and not returned within last second
        if dt_ping < dt_seen and dt_ping > self.max_ping_wait:
            logger.debug(
                '{0} last ping: {1} last seen: {2}'
                .format(peer, dt_ping, dt_seen))
            logger.debug(
                '{0} did not respond to ping, disconnecting {1}:{2}'
                .format(peer, peer.ip, peer.port))
            self.remove_peer(peer)
        elif min(dt_seen, dt_ping) > self.max_silence:
            # ping silent peer
            logger.debug('pinging silent peer {0}'.format(peer))

            with peer.lock:
                peer.send_Ping()

    def _connect_peers(self):
        num_peers = self.config.getint('network', 'num_peers')
        candidates = self.get_peer_candidates()
        if len(self.connected_peers) < num_peers:
            logger.debug('not enough peers: {0}'.format(
                len(self.connected_peers)))
            logger.debug('num candidates: {0}'.format(len(candidates)))
            if len(candidates):
                ip, port, node_id = candidates.pop()
                self.connect_peer(ip, port)
                # don't use this node again in case of connect error > remove
                self._known_peers.remove((ip, port, node_id))
            else:
                for peer in list(self.connected_peers):
                    with peer.lock:
                        peer.send_GetPeers()

    def loop_body(self):
        "check peer health every 10 seconds"
        for peer in list(self.connected_peers):
            self._check_alive(peer)
        self._connect_peers()

        for i in range(100):
            if not self.stopped():
                time.sleep(.1)

    def _start_peer(self, connection, ip, port):
        peer = Peer(connection, ip, port)
        peer.start()
        return peer

    def add_peer(self, connection, ip, port):
        # FIXME: should check existance first
        connection.settimeout(1)
        peer = self._start_peer(connection, ip, port)
        with self.lock:
            self.connected_peers.add(peer)
        return peer

peer_manager = PeerManager()


@receiver(signals.config_ready)
def config_peermanager(sender, **kwargs):
    peer_manager.configure(sender)


@receiver(signals.local_address_set)
def local_address_set_handler(sender, ip, port, **kwargs):
    local_addresses = []
    if ip == '0.0.0.0':
        for interface in netifaces.interfaces():
            ifaddresses = netifaces.ifaddresses(interface)
            if netifaces.AF_INET in ifaddresses:
                inets = ifaddresses[netifaces.AF_INET]
                for inet in inets:
                    local_addresses.append((inet['addr'], port))
    else:
        local_addresses = [(ip, port)]

    with peer_manager.lock:
        peer_manager.local_addresses.extend(local_addresses)


@receiver(signals.connection_accepted)
def connection_accepted_handler(sender, connection, ip, port, **kwargs):
    peer_manager.add_peer(connection, ip, port)


@receiver(signals.peers_data_requested)
def peers_data_requested_handler(sender, request_data, **kwargs):
    peers = peer_manager.get_known_peer_addresses()
    signals.peers_data_ready.send(None, requester=sender, ready_data=peers)


@receiver(signals.remote_chain_data_requested)
def remote_chain_data_requested_handler(sender, parents=[], count=1, **kwargs):
    for peer in peer_manager.connected_peers:
        peer.send_GetChain(parents, count)


@receiver(signals.disconnect_requested)
def disconnect_requested_handler(sender, **kwargs):
    peer = sender
    peer_manager.remove_peer(peer)


@receiver(signals.new_peer_received)
def new_peer_received_handler(sender, peer, **kwargs):
    ''' peer should be (ip, port, node_id)
    '''
    peer_manager.add_known_peer_address(*peer)
