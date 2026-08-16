#include "VistaPlayableHomeHUD.h"

#include "Engine/Canvas.h"
#include "Engine/Engine.h"
#include "Engine/Font.h"
#include "VistaEventSubsystem.h"
#include "VistaInteractable.h"
#include "VistaInteractionComponent.h"
#include "VistaPickupActor.h"
#include "VistaPlayableHomeCharacter.h"

namespace
{
FString FriendlyNameFromSemanticId(const FString& SemanticId)
{
    FString Leaf = SemanticId;
    int32 SeparatorIndex = INDEX_NONE;
    if (Leaf.FindChar(TEXT('#'), SeparatorIndex))
    {
        Leaf.LeftInline(SeparatorIndex, EAllowShrinking::No);
    }
    if (Leaf.FindLastChar(TEXT('/'), SeparatorIndex))
    {
        Leaf.RightChopInline(SeparatorIndex + 1, EAllowShrinking::No);
    }

    Leaf.ReplaceInline(TEXT("."), TEXT("_"), ESearchCase::CaseSensitive);
    Leaf.ReplaceInline(TEXT("-"), TEXT("_"), ESearchCase::CaseSensitive);
    TArray<FString> Tokens;
    Leaf.ParseIntoArray(Tokens, TEXT("_"), true);

    TArray<FString> FriendlyWords;
    for (FString Token : Tokens)
    {
        Token.TrimStartAndEndInline();
        const bool bStructuralToken =
            Token.Equals(TEXT("entity"), ESearchCase::IgnoreCase) ||
            Token.Equals(TEXT("anchor"), ESearchCase::IgnoreCase) ||
            Token.Equals(TEXT("actor"), ESearchCase::IgnoreCase) ||
            Token.Equals(TEXT("object"), ESearchCase::IgnoreCase) ||
            Token.Equals(TEXT("prop"), ESearchCase::IgnoreCase);
        if (Token.IsEmpty() || Token.IsNumeric() || bStructuralToken)
        {
            continue;
        }
        Token.ToLowerInline();
        Token[0] = FChar::ToUpper(Token[0]);
        FriendlyWords.Add(MoveTemp(Token));
    }
    return FriendlyWords.IsEmpty()
        ? FString(TEXT("Object"))
        : FString::Join(FriendlyWords, TEXT(" "));
}

FString FriendlyNameForActor(AActor* Actor)
{
    if (!IsValid(Actor) ||
        !Actor->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
    {
        return TEXT("Object");
    }
    return FriendlyNameFromSemanticId(
        IVistaInteractable::Execute_VistaGetSemanticId(Actor));
}

bool IsEnabledRuntimeValue(const FString& Value)
{
    return Value.Equals(TEXT("true"), ESearchCase::IgnoreCase) ||
           Value.Equals(TEXT("on"), ESearchCase::IgnoreCase) ||
           Value.Equals(TEXT("1"), ESearchCase::CaseSensitive);
}

bool IsToggleEnabled(AActor* Actor)
{
    if (!IsValid(Actor) ||
        !Actor->GetClass()->ImplementsInterface(UVistaInteractable::StaticClass()))
    {
        return false;
    }
    const FVistaEntityRuntimeState State =
        IVistaInteractable::Execute_VistaGetRuntimeState(Actor);
    const TCHAR* ToggleKeys[] = {TEXT("active"), TEXT("powered"), TEXT("on")};
    for (const TCHAR* Key : ToggleKeys)
    {
        if (const FString* Value = State.Values.Find(FName(Key)))
        {
            return IsEnabledRuntimeValue(*Value);
        }
    }
    return false;
}

FString BuildInteractionLabel(
    const AVistaPlayableHomeCharacter& Character,
    AActor* Target)
{
    if (!IsValid(Target))
    {
        return FString();
    }

    const AVistaPickupActor* Held = Character.GetHeldPickup();
    if (IsValid(Held) && Target != Held)
    {
        return FString::Printf(
            TEXT("Place %s"), *FriendlyNameFromSemanticId(Held->SemanticId));
    }

    const FString TargetName = FriendlyNameForActor(Target);
    switch (Character.GetDefaultInteractionAffordance(Target))
    {
        case EVistaAffordance::Open:
            return FString::Printf(TEXT("Open %s"), *TargetName);
        case EVistaAffordance::Close:
            return FString::Printf(TEXT("Close %s"), *TargetName);
        case EVistaAffordance::PickUp:
            return FString::Printf(TEXT("Pick Up %s"), *TargetName);
        case EVistaAffordance::Drop:
            return FString::Printf(TEXT("Drop %s"), *TargetName);
        case EVistaAffordance::Place:
            return FString::Printf(TEXT("Place %s"), *TargetName);
        case EVistaAffordance::Toggle:
            return IsToggleEnabled(Target)
                ? FString::Printf(TEXT("Turn Off %s"), *TargetName)
                : FString::Printf(TEXT("Turn On %s"), *TargetName);
        case EVistaAffordance::Sit:
            return FString::Printf(TEXT("Sit on %s"), *TargetName);
        case EVistaAffordance::Inspect:
        default:
            return FString::Printf(TEXT("Inspect %s"), *TargetName);
    }
}

FString CondenseGoal(const FString& PublicGoal)
{
    FString Goal = PublicGoal;
    Goal.ReplaceInline(TEXT("\r"), TEXT(" "), ESearchCase::CaseSensitive);
    Goal.ReplaceInline(TEXT("\n"), TEXT(" "), ESearchCase::CaseSensitive);
    while (Goal.ReplaceInline(TEXT("  "), TEXT(" "), ESearchCase::CaseSensitive) > 0)
    {
    }
    Goal.TrimStartAndEndInline();
    constexpr int32 MaxGoalCharacters = 92;
    if (Goal.Len() > MaxGoalCharacters)
    {
        Goal = Goal.Left(MaxGoalCharacters - 3) + TEXT("...");
    }
    return Goal;
}

FString ScenarioStatusLabel(EVistaEventStatus Status)
{
    switch (Status)
    {
        case EVistaEventStatus::Applying:
            return TEXT("STARTING");
        case EVistaEventStatus::Active:
            return TEXT("IN PROGRESS");
        case EVistaEventStatus::Succeeded:
            return TEXT("COMPLETE");
        case EVistaEventStatus::Failed:
            return TEXT("FAILED");
        case EVistaEventStatus::TimedOut:
            return TEXT("TIME EXPIRED");
        case EVistaEventStatus::Resetting:
            return TEXT("RESETTING");
        case EVistaEventStatus::Inactive:
        default:
            return FString();
    }
}

FLinearColor ScenarioStatusColor(EVistaEventStatus Status)
{
    switch (Status)
    {
        case EVistaEventStatus::Succeeded:
            return FLinearColor(0.43f, 0.70f, 0.52f, 1.0f);
        case EVistaEventStatus::Failed:
        case EVistaEventStatus::TimedOut:
            return FLinearColor(0.82f, 0.35f, 0.30f, 1.0f);
        default:
            return FLinearColor(0.78f, 0.61f, 0.32f, 1.0f);
    }
}
} // namespace

void AVistaPlayableHomeHUD::DrawHUD()
{
    Super::DrawHUD();
    if (!Canvas || !PlayerOwner || !GEngine)
    {
        return;
    }
    const AVistaPlayableHomeCharacter* Character =
        Cast<AVistaPlayableHomeCharacter>(PlayerOwner->GetPawn());
    if (!IsValid(Character))
    {
        return;
    }

    UFont* Font = GEngine->GetSmallFont();
    if (!IsValid(Font))
    {
        return;
    }

    const float UiScale = FMath::Clamp(Canvas->ClipY / 1080.0f, 0.75f, 1.5f);
    const FLinearColor Panel(0.035f, 0.038f, 0.041f, 0.86f);
    const FLinearColor Primary(0.92f, 0.90f, 0.85f, 1.0f);
    const FLinearColor Muted(0.61f, 0.60f, 0.56f, 1.0f);
    const FLinearColor Accent(0.78f, 0.61f, 0.32f, 1.0f);

    // A restrained reticle keeps the interaction surface game-like without
    // competing with the world or exposing implementation details.
    const float ReticleSize = 4.0f * UiScale;
    DrawRect(FLinearColor(0.92f, 0.90f, 0.85f, 0.78f),
             (Canvas->ClipX - ReticleSize) * 0.5f,
             (Canvas->ClipY - ReticleSize) * 0.5f,
             ReticleSize, ReticleSize);

    if (GetWorld())
    {
        const UVistaEventSubsystem* Events =
            GetWorld()->GetSubsystem<UVistaEventSubsystem>();
        if (IsValid(Events))
        {
            const EVistaEventStatus Status = Events->GetEventStatus();
            const FString Goal = CondenseGoal(Events->GetPublicGoal());
            const FString StatusLabel = ScenarioStatusLabel(Status);
            if (!Goal.IsEmpty() && !StatusLabel.IsEmpty())
            {
                const float Margin = 32.0f * UiScale;
                const float PanelWidth = FMath::Min(
                    620.0f * UiScale,
                    FMath::Max(280.0f * UiScale, Canvas->ClipX - Margin * 2.0f));
                const float PanelHeight = 78.0f * UiScale;
                DrawRect(Panel, Margin, Margin, PanelWidth, PanelHeight);
                DrawRect(Accent, Margin, Margin, 3.0f * UiScale, PanelHeight);
                DrawText(TEXT("OBJECTIVE"), Muted,
                         Margin + 18.0f * UiScale, Margin + 12.0f * UiScale,
                         Font, 0.82f * UiScale, false);
                DrawText(StatusLabel, ScenarioStatusColor(Status),
                         Margin + 112.0f * UiScale, Margin + 12.0f * UiScale,
                         Font, 0.82f * UiScale, false);
                DrawText(Goal, Primary,
                         Margin + 18.0f * UiScale, Margin + 39.0f * UiScale,
                         Font, UiScale, false);
            }
        }
    }

    const UVistaInteractionComponent* Interaction = Character->InteractionComponent;
    AActor* FocusedActor = IsValid(Interaction)
        ? Interaction->GetFocusedActor()
        : nullptr;
    const FString InteractionLabel = BuildInteractionLabel(
        *Character, FocusedActor);
    if (!InteractionLabel.IsEmpty())
    {
        const FString Prompt = FString::Printf(TEXT("[E]  %s"), *InteractionLabel);
        float TextWidth = 0.0f;
        float TextHeight = 0.0f;
        GetTextSize(Prompt, TextWidth, TextHeight, Font, UiScale);
        const float PanelWidth = FMath::Clamp(
            TextWidth + 46.0f * UiScale, 220.0f * UiScale, 560.0f * UiScale);
        const float PanelHeight = 44.0f * UiScale;
        const float PanelX = (Canvas->ClipX - PanelWidth) * 0.5f;
        const float PanelY = Canvas->ClipY - 76.0f * UiScale;
        DrawRect(Panel, PanelX, PanelY, PanelWidth, PanelHeight);
        DrawRect(Accent, PanelX, PanelY + PanelHeight - 2.0f * UiScale,
                 PanelWidth, 2.0f * UiScale);
        DrawText(Prompt, Primary,
                 PanelX + (PanelWidth - TextWidth) * 0.5f,
                 PanelY + (PanelHeight - TextHeight) * 0.5f,
                 Font, UiScale, false);
    }

    const AVistaPickupActor* Held = Character->GetHeldPickup();
    if (IsValid(Held))
    {
        const float Margin = 32.0f * UiScale;
        const float PanelWidth = 220.0f * UiScale;
        const float PanelHeight = 54.0f * UiScale;
        const float PanelY = Canvas->ClipY - Margin - PanelHeight;
        DrawRect(Panel, Margin, PanelY, PanelWidth, PanelHeight);
        DrawText(TEXT("CARRYING"), Muted,
                 Margin + 14.0f * UiScale, PanelY + 8.0f * UiScale,
                 Font, 0.75f * UiScale, false);
        DrawText(FriendlyNameFromSemanticId(Held->SemanticId), Primary,
                 Margin + 14.0f * UiScale, PanelY + 28.0f * UiScale,
                 Font, 0.92f * UiScale, false);
        DrawText(TEXT("[Q] DROP"), Accent,
                 Margin + 142.0f * UiScale, PanelY + 28.0f * UiScale,
                 Font, 0.75f * UiScale, false);
    }
}
