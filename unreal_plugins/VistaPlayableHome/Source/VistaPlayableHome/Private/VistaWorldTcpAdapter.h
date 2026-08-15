#pragma once

#include "CoreMinimal.h"

class FTcpListener;
class FSocket;
struct FIPv4Endpoint;

/** Loopback-only, one-frame-per-connection adapter for vista_world_action. */
class FVistaWorldTcpAdapter final
{
public:
    explicit FVistaWorldTcpAdapter(uint16 InPort);
    ~FVistaWorldTcpAdapter();

    bool Start();
    void Stop();

private:
    uint16 Port = 0;
    TUniquePtr<FTcpListener> Listener;

    bool HandleConnection(FSocket* Socket, const FIPv4Endpoint& RemoteEndpoint);
    static FString DispatchFrame(const FString& Frame);
};
