"""blueco - bascule d'écouteurs Bluetooth entre Linux et Android, sans réseau.

Linux tient le rôle de serveur RFCOMM/SPP (profil BlueZ) ; l'appli Android se
connecte en client. Le protocole est symétrique : n'importe lequel des deux
côtés peut envoyer une requête et l'autre répond (voir ``protocol``).
"""

from .protocol import PROTOCOL_VERSION, VERSION, ProtocolError, SPP_UUID

__all__ = ["PROTOCOL_VERSION", "VERSION", "ProtocolError", "SPP_UUID", "__version__"]

__version__ = "0.1.0"
