from server import models
from server import utils

import math
import time
import queue
import packet
from autobahn.twisted.websocket import WebSocketServerProtocol


class GameServerProtocol(WebSocketServerProtocol):
    def __init__(self):
        super().__init__()
        self._packet_queue: queue.Queue[tuple['GameServerProtocol', packet.Packet]] = queue.Queue()
        self._state: callable = None
        self._state = self.LOGIN
        self._actor: models.Actor = None
        self._player_target: list[float] = None
        self._last_delta_time_checked: float = None
        self._known_others: set['GameServerProtocol'] = set()

    def PLAY(self, sender: 'GameServerProtocol', p: packet.Packet):
        if p.action == packet.Action.Chat:
            if sender == self:
                self.broadcast(p, exclude_self=True)
            else:
                self.send_client(p)
        elif p.action == packet.Action.ModelData:
            self.send_client(p)
            if sender not in self._known_others:
                sender.onPacket(self, packet.ModelDataPacket(models.create_dict(self._actor)))
                self._known_others.add(sender)
        elif p.action == packet.Action.Target:
            self._player_target = p.payloads

    def LOGIN(self, sender: 'GameServerProtocol', p: packet.Packet):
        if p.action == packet.Action.Login:
            username, password = p.payloads
            if models.User.objects.filter(username=username, password=password).exists():
                user = models.User.objects.get(username=username)
                self._actor = models.Actor.objects.get(user=user)

                self.send_client(packet.OkPacket())
                self.broadcast(packet.ModelDataPacket(models.create_dict(self._actor)))
                self._state = self.PLAY
            else:
                self.send_client(packet.DenyPacket("Username or password incorrect"))

        elif p.action == packet.Action.Register:
            username, password = p.payloads
            if models.User.objects.filter(username=username).exists():
                self.send_client(packet.DenyPacket("This username is already taken"))
            else:
                user = models.User(username=username, password=password)
                user.save()
                player_entity = models.Entity(name=username)
                player_entity.save()
                player_ientity = models.InstancedEntity(entity=player_entity, x=0, y=0)
                player_ientity.save()
                player = models.Actor(instanced_entity=player_ientity, user=user)
                player.save()
                self.send_client(packet.OkPacket())

    def _update_position(self) -> bool:
        "Attempt to update the actor's position and return true only if the position was changed"
        if not self._player_target:
            return False
        pos = [self._actor.instanced_entity.x, self._actor.instanced_entity.y]

        now: float = time.time()
        delta_time: float = 1 / self.factory.tickrate
        if self._last_delta_time_checked:
            delta_time = now - self._last_delta_time_checked
        self._last_delta_time_checked = now

        # Use delta time to calculate distance to travel this time
        dist: float = 70 * delta_time
        
        # Early exit if we are already within an acceptable distance of the target
        if math.dist(pos, self._player_target) < dist:
            return False
        
        # Update our model if we're not already close enough to the target
        d_x, d_y = utils.direction_to(pos, self._player_target)
        self._actor.instanced_entity.x += d_x * dist
        self._actor.instanced_entity.y += d_y * dist
        self._actor.instanced_entity.save()

        return True

    def tick(self):
        # Process the next packet in the queue
        if not self._packet_queue.empty():
            s, p = self._packet_queue.get()
            self._state(s, p)
        # To do when there are no packets to process
        elif self._state == self.PLAY: 
            if self._update_position():
                self.broadcast(packet.ModelDataPacket(models.create_dict(self._actor)))

    def broadcast(self, p: packet.Packet, exclude_self: bool = False):
        for other in self.factory.players:
            if other == self and exclude_self:
                continue
            other.onPacket(self, p)

    # Override
    def onConnect(self, request):
        print(f"Client connecting: {request.peer}")

    # Override
    def onOpen(self):
        print(f"Websocket connection open.")

    # Override
    def onClose(self, wasClean, code, reason):
        self.factory.players.remove(self)
        print(f"Websocket connection closed{' unexpectedly' if not wasClean else ' cleanly'} with code {code}: {reason}")

    # Override
    def onMessage(self, payload, isBinary):
        decoded_payload = payload.decode('utf-8')

        try:
            p: packet.Packet = packet.from_json(decoded_payload)
        except Exception as e:
            print(f"Could not load message as packet: {e}. Message was: {payload.decode('utf8')}")

        self.onPacket(self, p)

    # Override
    def onClose(self, wasClean, code, reason):
        if self._actor:
            self._actor.save()
        self.factory.players.remove(self)
        print(f"Websocket connection closed{' unexpectedly' if not wasClean else ' cleanly'} with code {code}: {reason}")

    def onPacket(self, sender: 'GameServerProtocol', p: packet.Packet):
        self._packet_queue.put((sender, p))
        print(f"Queued packet: {p}")

    def send_client(self, p: packet.Packet):
        b = bytes(p)
        self.sendMessage(b)