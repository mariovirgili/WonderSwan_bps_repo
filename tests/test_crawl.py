# Offline test of scripts/crawl.py against a mocked GitHub API (needs py7zr). Run: python tests/test_crawl.py
import io, json, sys, shutil, zipfile, zlib, importlib.util, tempfile
from pathlib import Path
import py7zr
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
work = Path(tempfile.mkdtemp())
(work/'scripts').mkdir(); shutil.copy(SRC/'scripts/crawl.py', work/'scripts/crawl.py')
(work/'sources.json').write_text(json.dumps({"sources":[
  {"type":"repo","repo":"mariovirgili/WonderSwanColor-AI_Translation","default_language":"en"},
  {"type":"repo","repo":"PokemonGoDB/wonderswan-english-patches","default_language":"en"},
  {"type":"user","user":"Gand46","default_language":None}]}))
def num(n):
    out=bytearray()
    while True:
        x=n&0x7f; n>>=7
        if n==0: out.append(0x80|x); return bytes(out)
        out.append(x); n-=1
def bps(src_crc,dst_crc,src=4194304,dst=4194304,meta=b''):
    body=b'BPS1'+num(src)+num(dst)+num(len(meta))+meta+num(5)  # one dummy action byte
    body+=src_crc.to_bytes(4,'little')+dst_crc.to_bytes(4,'little')
    return body+(zlib.crc32(body)&0xffffffff).to_bytes(4,'little')
def ips():
    return b'PATCH'+(0x100).to_bytes(3,'big')+(3).to_bytes(2,'big')+b'abc'+(0x200).to_bytes(3,'big')+b'\0\0'+(16).to_bytes(2,'big')+b'Z'+b'EOF'
def zipb(files):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        for n,d in files.items(): z.writestr(n,d)
    return b.getvalue()
def sevenz(files):
    b=io.BytesIO()
    with py7zr.SevenZipFile(b,'w') as z:
        for n,d in files.items(): z.writestr(d,n)
    return b.getvalue()
P1=bps(0xF8A1DD2B,0xF3DB0917,meta=b'One Piece GB')
P2=bps(0x14ADBD4B,0x3E655169); P2s=bps(0x14ADBD4B,0xBC3C951B)
P3=bps(0x11111111,0x22222222)
IPS=ips()
assets={1:P1, 2:zipb({'Ring Infinity v1.1/ring-infinity.bps':P2,'Ring Infinity v1.1/ring-infinity-special.bps':P2s,'README.txt':b'x'}),
        3:zipb({'FF4 [T-En v0.9].bps':P3}), 4:sevenz({'Gioco (ITA)/patch_ITA.ips':IPS}), 5:b'not a patch at all'}
releases={
 "mariovirgili/WonderSwanColor-AI_Translation":[{"id":10,"tag_name":"ff4-v0.9","name":"Final Fantasy IV (WSC) English v0.9","html_url":"https://x/r/ff4","published_at":"2026-10-01T00:00:00Z","assets":[
    {"id":3,"name":"FF4_EN_v0.9.zip","size":len(assets[3]),"updated_at":"t1","browser_download_url":"u"},
    {"id":5,"name":"broken.bps","size":18,"updated_at":"t1","browser_download_url":"u"}]}],
 "PokemonGoDB/wonderswan-english-patches":[
   {"id":20,"tag_name":"grand-battle-v1.3","name":"One Piece: Grand Battle Swan Colosseum v1.3","html_url":"https://x/r/gb","assets":[
     {"id":1,"name":"grand-battle-v1.3.bps","size":len(P1),"updated_at":"t1","browser_download_url":"u"}]},
   {"id":21,"tag_name":"ring-infinity-v1.1","name":"Ring Infinity v1.1","html_url":"https://x/r/ri","assets":[
     {"id":2,"name":"ring-infinity-v1.1.zip","size":len(assets[2]),"updated_at":"t1","browser_download_url":"u"}]}],
 "Gand46/traduzioni":[{"id":30,"tag_name":"v1","name":"Gioco in italiano","html_url":"https://x/r/g","assets":[
     {"id":4,"name":"gioco_ITA.7z","size":len(assets[4]),"updated_at":"t1","browser_download_url":"u"}]}],
}
blobs={"b1":bps(0x33333333,0x44444444)}
trees={"Gand46/traduzioni":[{"path":"patches/Densha de Go!/densha-de-go.bps","type":"blob","sha":"b1","size":30},{"path":"README.md","type":"blob","sha":"r"}]}
spec=importlib.util.spec_from_file_location('crawl',work/'scripts/crawl.py'); c=importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
downloads=[]
def api(path):
    if path.startswith('/repos/') and '/git/trees/' in path:
        full='/'.join(path.split('/')[2:4]); return {"sha":"c0ffee","tree":trees.get(full,[]),"truncated":False}
    full='/'.join(path.split('/')[2:4]); return {"html_url":f"https://github.com/{full}","default_branch":"main"}
def api_list(path):
    if path.startswith('/users/Gand46/repos'): return [{"full_name":"Gand46/traduzioni","fork":False},{"full_name":"Gand46/forked","fork":True}]
    full='/'.join(path.split('/')[2:4]); return releases.get(full,[])
def http_get(url,accept=None,auth=True):
    downloads.append(url)
    if '/releases/assets/' in url: return assets[int(url.rsplit('/',1)[1])],{}
    if '/git/blobs/' in url: return blobs[url.rsplit('/',1)[1]],{}
    raise AssertionError(url)
c.api, c.api_list, c.http_get = api, api_list, http_get
assert c.main()==0
idx=json.loads((work/'index.json').read_text())
for e in idx['entries']:
    print(f"{e['game']!s:40} {e['variant']:28} v={e['version']!s:5} lang={e['language']!s:4} {e['format']} src={e['source_crc32']} dst={e['target_crc32']} ok={e.get('patch_checksum_ok')} -> {e['path']}")
print('count',idx['count'],'downloads',len(downloads))
gen1=idx['generated_at']
# second run: nothing new -> no downloads, no change
downloads.clear(); assert c.main()==0
idx2=json.loads((work/'index.json').read_text()); print('2nd run downloads',len(downloads),'changed', idx2['generated_at']!=gen1, 'count', idx2['count'])
# third run: special edition removed upstream (asset updated) + a source failing
assets[2]=zipb({'Ring Infinity v1.1/ring-infinity.bps':P2}); releases["PokemonGoDB/wonderswan-english-patches"][1]["assets"][0].update(updated_at="t2",size=len(assets[2]))
orig=api_list
def api_list2(path):
    if 'Gand46/traduzioni/releases' in path: raise OSError('simulated outage')
    return orig(path)
c.api_list=api_list2
downloads.clear(); assert c.main()==0
idx3=json.loads((work/'index.json').read_text())
print('3rd run downloads',len(downloads),'count',idx3['count'],'special still present:',any('special' in e['variant'] for e in idx3['entries']),
      'gand46 kept:',sum(e['origin']['source'].startswith('Gand46') for e in idx3['entries']))
print('files on disk:',sorted(p.relative_to(work).as_posix() for p in (work/'patches').rglob('*') if p.is_file()))
print((work/'PATCHES.md').read_text()[:900])
