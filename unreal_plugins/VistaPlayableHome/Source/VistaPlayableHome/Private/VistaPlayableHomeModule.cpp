#include "VistaPlayableHomeModule.h"

#include "VistaWorldTcpAdapter.h"

#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "Modules/ModuleManager.h"

IMPLEMENT_MODULE(FVistaPlayableHomeModule, VistaPlayableHome)

FVistaPlayableHomeModule::~FVistaPlayableHomeModule() = default;

void FVistaPlayableHomeModule::StartupModule()
{
    int32 RequestedPort = 0;
    if (!FParse::Value(FCommandLine::Get(), TEXT("VistaWorldPort="), RequestedPort))
    {
        return;
    }
    if (RequestedPort < 1024 || RequestedPort > 65535)
    {
        UE_LOG(LogTemp, Error, TEXT("-VistaWorldPort must be between 1024 and 65535"));
        return;
    }
    TcpAdapter = MakeUnique<FVistaWorldTcpAdapter>(static_cast<uint16>(RequestedPort));
    if (!TcpAdapter->Start())
    {
        UE_LOG(LogTemp, Error, TEXT("VISTA World loopback adapter failed to start"));
        TcpAdapter.Reset();
    }
}

void FVistaPlayableHomeModule::ShutdownModule()
{
    if (TcpAdapter)
    {
        TcpAdapter->Stop();
        TcpAdapter.Reset();
    }
}
