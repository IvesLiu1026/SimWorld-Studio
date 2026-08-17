#include "VistaPlayableHomeNaniteLibrary.h"

#include "AssetToolsModule.h"
#include "Containers/StringConv.h"
#include "Engine/StaticMesh.h"
#include "HAL/FileManager.h"
#include "IAssetTools.h"
#include "Materials/Material.h"
#include "Materials/MaterialInstanceConstant.h"
#include "Materials/MaterialInterface.h"
#include "MaterialEditingLibrary.h"
#include "Misc/PackageName.h"
#include "Misc/Paths.h"
#include "Serialization/JsonWriter.h"
#include "UObject/Package.h"
#include "UObject/SavePackage.h"

namespace VistaPlayableHomeNanite
{
constexpr const TCHAR* Schema =
    TEXT("simworld.vista.playable-home-native-nanite/v1");
constexpr const TCHAR* RevisionRoot = TEXT("/Game/VISTA/PlayableHome/");
constexpr int32 MaxParentDepth = 16;
constexpr int32 MaxMeshCount = 4096;

struct FMeshResult
{
    FString ObjectPath;
    TArray<FString> MaterialBlendModes;
    FString NanitePolicy;
    bool bNaniteEnabled = false;
};

bool IsPathInside(const FString& ObjectPath, const FString& LongPackageRoot)
{
    return ObjectPath == LongPackageRoot ||
        ObjectPath.StartsWith(LongPackageRoot + TEXT("/"), ESearchCase::CaseSensitive);
}

bool ValidateRevisionNamespace(const FString& Namespace, FString& OutError)
{
    if (!Namespace.StartsWith(RevisionRoot, ESearchCase::CaseSensitive) ||
        Namespace.EndsWith(TEXT("/"), ESearchCase::CaseSensitive) ||
        !FPackageName::IsValidLongPackageName(Namespace))
    {
        OutError = TEXT("INVALID_REVISION_NAMESPACE: expected /Game/VISTA/PlayableHome/<revision>");
        return false;
    }

    const FString Revision = Namespace.Mid(FCString::Strlen(RevisionRoot));
    if (Revision.IsEmpty() || Revision.Len() > 128 || Revision.Contains(TEXT("/")))
    {
        OutError = TEXT("INVALID_REVISION_NAMESPACE: revision must be one non-empty path segment");
        return false;
    }
    for (const TCHAR Character : Revision)
    {
        const bool bAllowed =
            (Character >= TEXT('A') && Character <= TEXT('Z')) ||
            (Character >= TEXT('a') && Character <= TEXT('z')) ||
            (Character >= TEXT('0') && Character <= TEXT('9')) ||
            Character == TEXT('_');
        if (!bAllowed)
        {
            OutError = TEXT("INVALID_REVISION_NAMESPACE: revision may contain only ASCII letters, digits, and underscore");
            return false;
        }
    }
    return true;
}

FString SanitizeAssetName(const FString& SourceObjectPath)
{
    const FString SourceName = FPackageName::ObjectPathToObjectName(SourceObjectPath);
    FString Result;
    Result.Reserve(SourceName.Len());
    bool bPreviousWasSeparator = false;
    for (const TCHAR Character : SourceName)
    {
        const bool bAllowed =
            (Character >= TEXT('A') && Character <= TEXT('Z')) ||
            (Character >= TEXT('a') && Character <= TEXT('z')) ||
            (Character >= TEXT('0') && Character <= TEXT('9')) ||
            Character == TEXT('_');
        if (bAllowed)
        {
            Result.AppendChar(Character);
            bPreviousWasSeparator = false;
        }
        else if (!bPreviousWasSeparator)
        {
            Result.AppendChar(TEXT('_'));
            bPreviousWasSeparator = true;
        }
    }
    while (Result.StartsWith(TEXT("_")))
    {
        Result.RightChopInline(1, EAllowShrinking::No);
    }
    while (Result.EndsWith(TEXT("_")))
    {
        Result.LeftChopInline(1, EAllowShrinking::No);
    }
    return Result;
}

uint32 RotateRight(const uint32 Value, const uint32 Count)
{
    return (Value >> Count) | (Value << (32U - Count));
}

/**
 * Return the first 64 bits of the standard SHA-256 digest as lower-case hex.
 *
 * UE's generic platform SHA-256 helper deliberately asserts on platforms
 * without a platform override (including the UE 5.7 Linux build used by this
 * commandlet). Material names are a persistence contract, so substituting a
 * different hash would also make Python and native attempts disagree. This
 * compact single-shot implementation keeps the exact hashlib.sha256 UTF-8
 * contract without adding an OpenSSL runtime dependency to the editor plugin.
 */
bool SourcePathDigest16(const FString& SourceObjectPath, FString& OutDigest)
{
    const FTCHARToUTF8 Utf8(*SourceObjectPath);

    TArray<uint8> Message;
    Message.Reserve(Utf8.Length() + 72);
    Message.Append(
        reinterpret_cast<const uint8*>(Utf8.Get()),
        Utf8.Length());
    const uint64 MessageBitCount = static_cast<uint64>(Utf8.Length()) * 8ULL;
    Message.Add(0x80U);
    while ((Message.Num() % 64) != 56)
    {
        Message.Add(0U);
    }
    for (int32 Shift = 56; Shift >= 0; Shift -= 8)
    {
        Message.Add(static_cast<uint8>(MessageBitCount >> Shift));
    }

    static constexpr uint32 RoundConstants[64] = {
        0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U,
        0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
        0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
        0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
        0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
        0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
        0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
        0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
        0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
        0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
        0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U,
        0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
        0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U,
        0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
        0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
        0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U,
    };

    uint32 State[8] = {
        0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
        0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U,
    };
    for (int32 BlockOffset = 0; BlockOffset < Message.Num(); BlockOffset += 64)
    {
        uint32 Words[64] = {};
        for (int32 WordIndex = 0; WordIndex < 16; ++WordIndex)
        {
            const int32 ByteIndex = BlockOffset + WordIndex * 4;
            Words[WordIndex] =
                (static_cast<uint32>(Message[ByteIndex]) << 24U) |
                (static_cast<uint32>(Message[ByteIndex + 1]) << 16U) |
                (static_cast<uint32>(Message[ByteIndex + 2]) << 8U) |
                static_cast<uint32>(Message[ByteIndex + 3]);
        }
        for (int32 WordIndex = 16; WordIndex < 64; ++WordIndex)
        {
            const uint32 Previous15 = Words[WordIndex - 15];
            const uint32 Previous2 = Words[WordIndex - 2];
            const uint32 Sigma0 =
                RotateRight(Previous15, 7U) ^
                RotateRight(Previous15, 18U) ^
                (Previous15 >> 3U);
            const uint32 Sigma1 =
                RotateRight(Previous2, 17U) ^
                RotateRight(Previous2, 19U) ^
                (Previous2 >> 10U);
            Words[WordIndex] = Words[WordIndex - 16] + Sigma0 +
                Words[WordIndex - 7] + Sigma1;
        }

        uint32 A = State[0];
        uint32 B = State[1];
        uint32 C = State[2];
        uint32 D = State[3];
        uint32 E = State[4];
        uint32 F = State[5];
        uint32 G = State[6];
        uint32 H = State[7];
        for (int32 Round = 0; Round < 64; ++Round)
        {
            const uint32 UpperSigma1 =
                RotateRight(E, 6U) ^ RotateRight(E, 11U) ^ RotateRight(E, 25U);
            const uint32 Choice = (E & F) ^ ((~E) & G);
            const uint32 Temporary1 = H + UpperSigma1 + Choice +
                RoundConstants[Round] + Words[Round];
            const uint32 UpperSigma0 =
                RotateRight(A, 2U) ^ RotateRight(A, 13U) ^ RotateRight(A, 22U);
            const uint32 Majority = (A & B) ^ (A & C) ^ (B & C);
            const uint32 Temporary2 = UpperSigma0 + Majority;
            H = G;
            G = F;
            F = E;
            E = D + Temporary1;
            D = C;
            C = B;
            B = A;
            A = Temporary1 + Temporary2;
        }
        State[0] += A;
        State[1] += B;
        State[2] += C;
        State[3] += D;
        State[4] += E;
        State[5] += F;
        State[6] += G;
        State[7] += H;
    }

    OutDigest = FString::Printf(TEXT("%08x%08x"), State[0], State[1]);
    return OutDigest.Len() == 16;
}

FString BlendModeName(const EBlendMode BlendMode)
{
    switch (BlendMode)
    {
    case BLEND_Opaque:
        return TEXT("BLEND_OPAQUE");
    case BLEND_Masked:
        return TEXT("BLEND_MASKED");
    case BLEND_Translucent:
        return TEXT("BLEND_TRANSLUCENT");
    case BLEND_Additive:
        return TEXT("BLEND_ADDITIVE");
    case BLEND_Modulate:
        return TEXT("BLEND_MODULATE");
    case BLEND_AlphaComposite:
        return TEXT("BLEND_ALPHA_COMPOSITE");
    case BLEND_AlphaHoldout:
        return TEXT("BLEND_ALPHA_HOLDOUT");
    case BLEND_TranslucentColoredTransmittance:
        return TEXT("BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE");
    default:
        return FString::Printf(TEXT("BLEND_UNKNOWN_%d"), static_cast<int32>(BlendMode));
    }
}

FString ErrorJson(const FString& Error)
{
    FString ErrorCode = Error;
    FString ErrorMessage = Error;
    int32 SeparatorIndex = INDEX_NONE;
    if (Error.FindChar(TEXT(':'), SeparatorIndex) && SeparatorIndex > 0)
    {
        ErrorCode = Error.Left(SeparatorIndex);
        ErrorMessage = Error.Mid(SeparatorIndex + 1).TrimStartAndEnd();
    }

    FString Output;
    const TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer =
        TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Output);
    Writer->WriteObjectStart();
    Writer->WriteValue(TEXT("schema_version"), Schema);
    Writer->WriteValue(TEXT("status"), TEXT("failed"));
    Writer->WriteObjectStart(TEXT("error"));
    Writer->WriteValue(TEXT("code"), ErrorCode);
    Writer->WriteValue(TEXT("message"), ErrorMessage);
    Writer->WriteObjectEnd();
    Writer->WriteArrayStart(TEXT("results"));
    Writer->WriteArrayEnd();
    Writer->WriteObjectEnd();
    Writer->Close();
    return Output;
}

FString SuccessJson(TArray<FMeshResult> Results)
{
    Results.Sort([](const FMeshResult& Left, const FMeshResult& Right)
    {
        return Left.ObjectPath < Right.ObjectPath;
    });

    FString Output;
    const TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer =
        TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Output);
    Writer->WriteObjectStart();
    Writer->WriteValue(TEXT("schema_version"), Schema);
    Writer->WriteValue(TEXT("status"), TEXT("success"));
    Writer->WriteArrayStart(TEXT("results"));
    for (const FMeshResult& Result : Results)
    {
        Writer->WriteObjectStart();
        Writer->WriteValue(TEXT("object_path"), Result.ObjectPath);
        Writer->WriteArrayStart(TEXT("material_blend_modes"));
        for (const FString& BlendMode : Result.MaterialBlendModes)
        {
            Writer->WriteValue(BlendMode);
        }
        Writer->WriteArrayEnd();
        Writer->WriteValue(TEXT("nanite_policy"), Result.NanitePolicy);
        Writer->WriteValue(TEXT("nanite_enabled"), Result.bNaniteEnabled);
        Writer->WriteObjectEnd();
    }
    Writer->WriteArrayEnd();
    Writer->WriteObjectEnd();
    Writer->Close();
    return Output;
}

class FPolicyContext final
{
public:
    explicit FPolicyContext(const FString& InNamespace)
        : Namespace(InNamespace)
        , InternalMaterialPath(InNamespace + TEXT("/Internal/Materials"))
        , AssetTools(FAssetToolsModule::GetModule().Get())
    {
    }

    bool FinalizeMesh(const FString& MeshObjectPath, FMeshResult& OutResult, FString& OutError)
    {
        FText PathReason;
        if (!MeshObjectPath.Contains(TEXT("."), ESearchCase::CaseSensitive) ||
            !FPackageName::IsValidObjectPath(MeshObjectPath, &PathReason) ||
            !IsPathInside(MeshObjectPath, Namespace + TEXT("/Assets")))
        {
            OutError = FString::Printf(
                TEXT("INVALID_MESH_OBJECT_PATH: %s"), *MeshObjectPath);
            return false;
        }

        UStaticMesh* Mesh = LoadObject<UStaticMesh>(nullptr, *MeshObjectPath);
        if (!IsValid(Mesh) || Mesh->GetPathName() != MeshObjectPath)
        {
            OutError = FString::Printf(
                TEXT("STATIC_MESH_LOAD_FAILED: %s"), *MeshObjectPath);
            return false;
        }

        Mesh->Modify();
        if (!LoadedMeshSet.Contains(Mesh))
        {
            LoadedMeshSet.Add(Mesh);
            LoadedMeshes.Add(Mesh);
        }
        bool bAllOpaqueOrMasked = true;
        TArray<UMaterial*> PrivateRoots;
        TSet<UMaterial*> UniqueRoots;
        TArray<FStaticMaterial>& Slots = Mesh->GetStaticMaterials();
        for (int32 SlotIndex = 0; SlotIndex < Slots.Num(); ++SlotIndex)
        {
            UMaterialInterface* SlotMaterial = Slots[SlotIndex].MaterialInterface;
            if (!IsValid(SlotMaterial))
            {
                OutError = FString::Printf(
                    TEXT("NULL_MATERIAL_SLOT: %s[%d]"), *MeshObjectPath, SlotIndex);
                return false;
            }

            const EBlendMode BlendMode = SlotMaterial->GetBlendMode();
            const bool bTwoSided = SlotMaterial->IsTwoSided();
            OutResult.MaterialBlendModes.Add(BlendModeName(BlendMode));
            bAllOpaqueOrMasked = bAllOpaqueOrMasked &&
                (BlendMode == BLEND_Opaque || BlendMode == BLEND_Masked);

            UMaterialInterface* PrivateSlotMaterial = nullptr;
            UMaterial* PrivateRoot = nullptr;
            if (IsPathInside(SlotMaterial->GetPathName(), Namespace))
            {
                if (UMaterialInstanceConstant* LocalInstance =
                        Cast<UMaterialInstanceConstant>(SlotMaterial))
                {
                    UMaterialInterface* PrivateParent = nullptr;
                    TSet<FString> Visiting;
                    if (!ResolvePrivateChain(
                            LocalInstance->Parent, 0, Visiting,
                            PrivateParent, PrivateRoot, OutError))
                    {
                        return false;
                    }
                    if (LocalInstance->Parent != PrivateParent)
                    {
                        LocalInstance->Modify();
                        LocalInstance->SetParentEditorOnly(PrivateParent);
                        LocalInstance->PostEditChange();
                    }
                    PrivateSlotMaterial = LocalInstance;
                    MarkForSave(LocalInstance);
                }
                else if (Cast<UMaterial>(SlotMaterial) != nullptr &&
                         IsPathInside(SlotMaterial->GetPathName(), InternalMaterialPath))
                {
                    PrivateSlotMaterial = SlotMaterial;
                    PrivateRoot = CastChecked<UMaterial>(SlotMaterial);
                    MarkForSave(SlotMaterial);
                }
                else
                {
                    TSet<FString> Visiting;
                    if (!ResolvePrivateChain(
                            SlotMaterial, 0, Visiting,
                            PrivateSlotMaterial, PrivateRoot, OutError))
                    {
                        return false;
                    }
                    Slots[SlotIndex].MaterialInterface = PrivateSlotMaterial;
                }
            }
            else
            {
                TSet<FString> Visiting;
                if (!ResolvePrivateChain(
                        SlotMaterial, 0, Visiting,
                        PrivateSlotMaterial, PrivateRoot, OutError))
                {
                    return false;
                }
                Slots[SlotIndex].MaterialInterface = PrivateSlotMaterial;
            }

            if (!IsValid(PrivateSlotMaterial) || !IsValid(PrivateRoot) ||
                !IsPathInside(PrivateRoot->GetPathName(), InternalMaterialPath))
            {
                OutError = FString::Printf(
                    TEXT("PRIVATE_MATERIAL_CHAIN_UNPROVEN: %s[%d]"),
                    *MeshObjectPath, SlotIndex);
                return false;
            }
            if (PrivateSlotMaterial->GetBlendMode() != BlendMode ||
                PrivateSlotMaterial->IsTwoSided() != bTwoSided)
            {
                OutError = FString::Printf(
                    TEXT("MATERIAL_OVERRIDE_PRESERVATION_FAILED: %s[%d]"),
                    *MeshObjectPath, SlotIndex);
                return false;
            }
            if (!UniqueRoots.Contains(PrivateRoot))
            {
                UniqueRoots.Add(PrivateRoot);
                PrivateRoots.Add(PrivateRoot);
            }
        }

        bool bUsageProven = bAllOpaqueOrMasked && Slots.Num() > 0;
        if (bUsageProven)
        {
            for (UMaterial* PrivateRoot : PrivateRoots)
            {
                if (!EnsureNaniteUsage(PrivateRoot, OutError))
                {
                    bUsageProven = false;
                    break;
                }
            }
        }

        if (bAllOpaqueOrMasked && !bUsageProven)
        {
            OutError = FString::Printf(
                TEXT("NANITE_USAGE_UNPROVEN: %s"), *MeshObjectPath);
            return false;
        }

        const bool bEnableNanite = bAllOpaqueOrMasked && bUsageProven;
        FMeshNaniteSettings Settings = Mesh->GetNaniteSettings();
        Settings.bEnabled = bEnableNanite;
        Mesh->SetNaniteSettings(Settings);
        Mesh->PostEditChange();
        MarkForSave(Mesh);

        if (Mesh->GetNaniteSettings().bEnabled != bEnableNanite)
        {
            OutError = FString::Printf(
                TEXT("NANITE_STATE_MISMATCH: %s"), *MeshObjectPath);
            return false;
        }

        OutResult.ObjectPath = MeshObjectPath;
        OutResult.NanitePolicy = bAllOpaqueOrMasked
            ? TEXT("eligible_static_opaque")
            : TEXT("disabled_nonopaque_material");
        OutResult.bNaniteEnabled = Mesh->GetNaniteSettings().bEnabled;
        return true;
    }

    bool SaveAll(FString& OutError)
    {
        for (UObject* Asset : AssetsToSave)
        {
            if (!IsValid(Asset) || !IsValid(Asset->GetPackage()) ||
                !IsPathInside(Asset->GetPathName(), Namespace))
            {
                OutError = TEXT("PRIVATE_ASSET_SAVE_SCOPE_VIOLATION");
                return false;
            }

            UPackage* Package = Asset->GetPackage();
            Package->MarkPackageDirty();
            const FString Filename = FPackageName::LongPackageNameToFilename(
                Package->GetName(), FPackageName::GetAssetPackageExtension());
            const FString Directory = FPaths::GetPath(Filename);
            if (!IFileManager::Get().DirectoryExists(*Directory) &&
                !IFileManager::Get().MakeDirectory(*Directory, true))
            {
                OutError = FString::Printf(
                    TEXT("PACKAGE_DIRECTORY_CREATE_FAILED: %s"), *Directory);
                return false;
            }
            FSavePackageArgs SaveArgs;
            SaveArgs.TopLevelFlags = RF_Public | RF_Standalone;
            SaveArgs.SaveFlags = SAVE_NoError;
            SaveArgs.bSlowTask = false;
            if (!UPackage::SavePackage(Package, nullptr, *Filename, SaveArgs))
            {
                OutError = FString::Printf(
                    TEXT("PACKAGE_SAVE_FAILED: %s"), *Package->GetName());
                return false;
            }
        }
        return true;
    }

    /** Persist a disabled state for every mesh touched by a failed batch. */
    bool FailClosedSave(FString& OutError)
    {
        for (UStaticMesh* Mesh : LoadedMeshes)
        {
            if (!IsValid(Mesh))
            {
                OutError = TEXT("FAIL_CLOSED_MESH_INVALID");
                return false;
            }
            Mesh->Modify();
            FMeshNaniteSettings Settings = Mesh->GetNaniteSettings();
            Settings.bEnabled = false;
            Mesh->SetNaniteSettings(Settings);
            Mesh->PostEditChange();
            MarkForSave(Mesh);
        }
        return SaveAll(OutError);
    }

private:
    bool ResolvePrivateChain(
        UMaterialInterface* Source,
        const int32 Depth,
        TSet<FString>& Visiting,
        UMaterialInterface*& OutPrivateMaterial,
        UMaterial*& OutPrivateRoot,
        FString& OutError)
    {
        OutPrivateMaterial = nullptr;
        OutPrivateRoot = nullptr;
        if (!IsValid(Source))
        {
            OutError = TEXT("MATERIAL_PARENT_MISSING");
            return false;
        }
        if (Depth > MaxParentDepth)
        {
            OutError = FString::Printf(
                TEXT("MATERIAL_PARENT_DEPTH_EXCEEDED: %s"), *Source->GetPathName());
            return false;
        }

        const FString SourcePath = Source->GetPathName();
        if (Visiting.Contains(SourcePath))
        {
            OutError = FString::Printf(
                TEXT("MATERIAL_PARENT_CYCLE: %s"), *SourcePath);
            return false;
        }
        Visiting.Add(SourcePath);

        UMaterialInterface* PrivateParent = nullptr;
        UMaterial* PrivateRoot = nullptr;
        const UMaterialInstanceConstant* SourceInstance =
            Cast<UMaterialInstanceConstant>(Source);
        if (SourceInstance != nullptr)
        {
            if (!ResolvePrivateChain(
                    SourceInstance->Parent, Depth + 1, Visiting,
                    PrivateParent, PrivateRoot, OutError))
            {
                Visiting.Remove(SourcePath);
                return false;
            }
        }
        else if (Cast<UMaterial>(Source) == nullptr)
        {
            Visiting.Remove(SourcePath);
            OutError = FString::Printf(
                TEXT("UNSUPPORTED_MATERIAL_INTERFACE: %s"), *SourcePath);
            return false;
        }

        UMaterialInterface* PrivateMaterial = Source;
        if (!IsPathInside(SourcePath, InternalMaterialPath))
        {
            if (UMaterialInterface** Cached = PrivateBySourcePath.Find(SourcePath))
            {
                PrivateMaterial = *Cached;
            }
            else
            {
                FString Digest;
                const FString SourceName = SanitizeAssetName(SourcePath);
                if (SourceName.IsEmpty() || !SourcePathDigest16(SourcePath, Digest))
                {
                    Visiting.Remove(SourcePath);
                    OutError = FString::Printf(
                        TEXT("MATERIAL_PRIVATE_NAME_FAILED: %s"), *SourcePath);
                    return false;
                }
                const FString AssetName = FString::Printf(
                    TEXT("VISTA_%s_%s"), *SourceName, *Digest);
                const FString PrivateObjectPath = FString::Printf(
                    TEXT("%s/%s.%s"),
                    *InternalMaterialPath, *AssetName, *AssetName);
                PrivateMaterial = LoadObject<UMaterialInterface>(
                    nullptr, *PrivateObjectPath);
                if (!IsValid(PrivateMaterial))
                {
                    PrivateMaterial = Cast<UMaterialInterface>(
                        AssetTools.DuplicateAsset(
                            AssetName, InternalMaterialPath, Source));
                }
                if (!IsValid(PrivateMaterial) ||
                    PrivateMaterial->GetClass() != Source->GetClass() ||
                    PrivateMaterial->GetPathName() != PrivateObjectPath)
                {
                    Visiting.Remove(SourcePath);
                    OutError = FString::Printf(
                        TEXT("MATERIAL_DUPLICATE_FAILED: %s"), *SourcePath);
                    return false;
                }
                PrivateBySourcePath.Add(SourcePath, PrivateMaterial);
            }
        }

        if (UMaterialInstanceConstant* PrivateInstance =
                Cast<UMaterialInstanceConstant>(PrivateMaterial))
        {
            if (!IsValid(PrivateParent))
            {
                Visiting.Remove(SourcePath);
                OutError = FString::Printf(
                    TEXT("PRIVATE_INSTANCE_PARENT_MISSING: %s"), *SourcePath);
                return false;
            }
            if (PrivateInstance->Parent != PrivateParent)
            {
                PrivateInstance->Modify();
                PrivateInstance->SetParentEditorOnly(PrivateParent);
                PrivateInstance->PostEditChange();
            }
            MarkForSave(PrivateInstance);
            OutPrivateRoot = PrivateRoot;
        }
        else
        {
            UMaterial* Root = Cast<UMaterial>(PrivateMaterial);
            if (!IsValid(Root))
            {
                Visiting.Remove(SourcePath);
                OutError = FString::Printf(
                    TEXT("PRIVATE_ROOT_TYPE_MISMATCH: %s"), *SourcePath);
                return false;
            }
            MarkForSave(Root);
            OutPrivateRoot = Root;
        }

        OutPrivateMaterial = PrivateMaterial;
        Visiting.Remove(SourcePath);
        return IsValid(OutPrivateRoot);
    }

    bool EnsureNaniteUsage(UMaterial* PrivateRoot, FString& OutError)
    {
        if (!IsValid(PrivateRoot) ||
            !IsPathInside(PrivateRoot->GetPathName(), InternalMaterialPath))
        {
            OutError = TEXT("NANITE_USAGE_SCOPE_VIOLATION");
            return false;
        }
        if (!UMaterialEditingLibrary::HasMaterialUsage(
                PrivateRoot, MATUSAGE_Nanite))
        {
            PrivateRoot->Modify();
            bool bNeedsRecompile = false;
            if (!UMaterialEditingLibrary::SetMaterialUsage(
                    PrivateRoot, MATUSAGE_Nanite, bNeedsRecompile))
            {
                OutError = FString::Printf(
                    TEXT("NANITE_USAGE_SET_FAILED: %s"),
                    *PrivateRoot->GetPathName());
                return false;
            }
            PrivateRoot->PostEditChange();
        }
        MarkForSave(PrivateRoot);
        if (!UMaterialEditingLibrary::HasMaterialUsage(
                PrivateRoot, MATUSAGE_Nanite))
        {
            OutError = FString::Printf(
                TEXT("NANITE_USAGE_UNPROVEN: %s"),
                *PrivateRoot->GetPathName());
            return false;
        }
        return true;
    }

    void MarkForSave(UObject* Asset)
    {
        if (IsValid(Asset) && !SaveSet.Contains(Asset))
        {
            SaveSet.Add(Asset);
            AssetsToSave.Add(Asset);
        }
    }

    FString Namespace;
    FString InternalMaterialPath;
    IAssetTools& AssetTools;
    TMap<FString, UMaterialInterface*> PrivateBySourcePath;
    TSet<UStaticMesh*> LoadedMeshSet;
    TArray<UStaticMesh*> LoadedMeshes;
    TSet<UObject*> SaveSet;
    TArray<UObject*> AssetsToSave;
};
} // namespace VistaPlayableHomeNanite

FString UVistaPlayableHomeNaniteLibrary::FinalizeNanitePolicies(
    const FString& RevisionNamespace,
    const TArray<FString>& MeshObjectPaths)
{
    using namespace VistaPlayableHomeNanite;

    FString Error;
    if (!ValidateRevisionNamespace(RevisionNamespace, Error))
    {
        return ErrorJson(Error);
    }
    if (MeshObjectPaths.IsEmpty() || MeshObjectPaths.Num() > MaxMeshCount)
    {
        return ErrorJson(TEXT("INVALID_MESH_COUNT: expected 1..4096 object paths"));
    }

    TSet<FString> SeenPaths;
    for (const FString& MeshObjectPath : MeshObjectPaths)
    {
        if (MeshObjectPath.IsEmpty() || SeenPaths.Contains(MeshObjectPath))
        {
            return ErrorJson(FString::Printf(
                TEXT("DUPLICATE_OR_EMPTY_MESH_OBJECT_PATH: %s"),
                *MeshObjectPath));
        }
        SeenPaths.Add(MeshObjectPath);
    }

    FPolicyContext Context(RevisionNamespace);
    TArray<FMeshResult> Results;
    Results.Reserve(MeshObjectPaths.Num());
    for (const FString& MeshObjectPath : MeshObjectPaths)
    {
        FMeshResult& Result = Results.AddDefaulted_GetRef();
        if (!Context.FinalizeMesh(MeshObjectPath, Result, Error))
        {
            const FString PolicyError = Error;
            FString PersistenceError;
            if (!Context.FailClosedSave(PersistenceError))
            {
                return ErrorJson(FString::Printf(
                    TEXT("FAIL_CLOSED_SAVE_FAILED: %s; %s"),
                    *PolicyError, *PersistenceError));
            }
            return ErrorJson(PolicyError);
        }
    }
    if (!Context.SaveAll(Error))
    {
        const FString SaveError = Error;
        FString PersistenceError;
        if (!Context.FailClosedSave(PersistenceError))
        {
            return ErrorJson(FString::Printf(
                TEXT("FAIL_CLOSED_SAVE_FAILED: %s; %s"),
                *SaveError, *PersistenceError));
        }
        return ErrorJson(SaveError);
    }
    return SuccessJson(Results);
}
