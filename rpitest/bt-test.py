# file: rfcomm-server.py
# auth: Albert Huang <albert@csail.mit.edu>
# desc: simple demonstration of a server application that uses RFCOMM sockets
#
# $Id: rfcomm-server.py 518 2007-08-10 07:20:07Z albert $
#!/usr/bin
import bluetooth
import os
#from bluetooth import *
client_sock = None
client_sock = None
os.system("sudo hciconfig hci0 piscan")
server_sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
server_sock.bind(("",1))
server_sock.listen(1)
port = server_sock.getsockname()[1]
uuid = "94f39d29-7d6d-437d-973b-fba39e49d4ee"
bluetooth.advertise_service(server_sock, "MDPGrp7", service_id=uuid, service_classes=[ uuid, bluetooth.SERIAL_PORT_CLASS], profiles=[bluetooth.SERIAL_PORT_PROFILE])
print("Waiting for connection on RFCOMM channel %d" % port)
client_sock, client_info = server_sock.accept()
print("Accepted connection from ", client_info)
try:
    while True:
        print ("In while loop...")
        data = client_sock.recv(1024)
        if len(data) == 0:  break
        print("Received [%s]" % data)
        #client_sock.send(data + " i am pi!")
        #self.logger.debug(tmp)
        client_sock.send(data)
        client_sock.send(data + b'\r\n')
        #self.logger.debug(f"Received from Android: {message}")

except IOError:
   pass
print("disconnected")
client_sock.close()
server_sock.close()
print("all done")
