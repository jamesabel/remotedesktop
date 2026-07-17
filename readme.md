# Remote Desktop

This is a Python client/server application that provide remote desktop
for Windows computers on the same LAN. It offers autodiscovery and 
connection.

Connections to the desktop screen, keyboard, mouse, and clipboard are made.
Other connections are not provide, such as shared drives, devices, nor 
multimedia (e.g., audio)

The client and server apps are both run on their respective computers.
The client requests connection to the server and for the initial connection 
the user on the server side must permit the connection. After that, 
the client can connect to the server whenever the server is running
without the server having to provide permission.

This does not use Windows RDP nor rely on any Microsoft based authentication.

