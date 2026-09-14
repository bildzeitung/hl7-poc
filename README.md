# PoC for HL7 Messages

This project explores HL7 messages.

## Infrastructure

* Google SimHospital -- HL7 events
* Microsoft Azure Service Bus Simulator

## Services

* listener: Receive messages from SimHospital; transform them into a canonical
            format and then send them into the service bus

* worker: Pull messages from the service bus and perform some trivial task
          according the particular message type received

## Local Service Bus emulator

`docker-compose.yml` mounts `servicebus-config.json` into the emulator, declaring the
`hl7-events` queue (sessions + duplicate detection enabled; see the file for details).
A local `hl7listener`/`hl7worker` should connect with the emulator's fixed developer
connection string:

```
Endpoint=sb://localhost;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=SAS_KEY_VALUE;UseDevelopmentEmulator=true;
```
