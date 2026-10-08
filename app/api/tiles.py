"""MapLibre Terrain-RGB tile server."""

from fastapi import APIRouter, Response

router = APIRouter(prefix="/tiles/terrain", tags=["tiles"])


@router.get("/{z}/{x}/{y}.png")
def get_terrain_tile(z: int, x: int, y: int):
    """Serve Terrain-RGB tile for 3D elevation rendering."""
    # 1x1 transparent PNG
    png_1x1 = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15"
        b"\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return Response(content=png_1x1, media_type="image/png")
