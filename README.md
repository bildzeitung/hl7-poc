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
