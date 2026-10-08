"""Map layer download and stream router (ARCHITECTURE.md Section 7)."""

from fastapi import APIRouter, Response

router = APIRouter(prefix="/jobs/{job_id}/layers", tags=["layers"])


@router.get("/{layer_name}")
def get_layer(job_id: str, layer_name: str):
    """Retrieve layer GeoJSON feature collection or raster stream."""
    if layer_name.endswith(".png") or layer_name in ("s1_pre", "s1_post", "flood_probability", "uncertainty"):
        # 1x1 transparent PNG placeholder
        png_1x1 = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15"
            b"\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return Response(content=png_1x1, media_type="image/png")

    # Vector GeoJSON
    return {
        "type": "FeatureCollection",
        "name": layer_name,
        "job_id": job_id,
        "features": []
    }
