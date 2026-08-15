#include "VistaPlayableHomeTypes.h"

FVistaInteractionResult FVistaInteractionResult::Success(
    const FString& InSemanticId,
    const FVistaEntityRuntimeState& InState,
    FName InCode)
{
    FVistaInteractionResult Result;
    Result.Status = EVistaInteractionStatus::Succeeded;
    Result.Code = InCode;
    Result.SemanticId = InSemanticId;
    Result.State = InState;
    return Result;
}

FVistaInteractionResult FVistaInteractionResult::Failure(
    EVistaInteractionStatus InStatus,
    FName InCode,
    const FString& InSemanticId)
{
    FVistaInteractionResult Result;
    Result.Status = InStatus;
    Result.Code = InCode;
    Result.SemanticId = InSemanticId;
    return Result;
}
