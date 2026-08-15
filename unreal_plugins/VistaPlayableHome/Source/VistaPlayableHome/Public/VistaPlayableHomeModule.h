#pragma once

#include "Modules/ModuleManager.h"

class FVistaWorldTcpAdapter;

class FVistaPlayableHomeModule final : public IModuleInterface
{
public:
    virtual ~FVistaPlayableHomeModule() override;
    virtual void StartupModule() override;
    virtual void ShutdownModule() override;

private:
    TUniquePtr<FVistaWorldTcpAdapter> TcpAdapter;
};
