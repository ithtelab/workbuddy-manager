using System.Text;

namespace WorkBuddySetup;

/// <summary>
/// 安装包数据紧跟在 setup.exe 末尾：
/// [zip 字节][8 字节长度][8 字节魔数 WBPKG001]
/// </summary>
internal static class Payload
{
    private static readonly byte[] Magic = Encoding.ASCII.GetBytes("WBPKG001");

    public static BoundedStream? Open()
    {
        var path = Environment.ProcessPath ?? Application.ExecutablePath;
        var info = new FileInfo(path);
        if (!info.Exists || info.Length < 32) return null;

        using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        fs.Seek(-16, SeekOrigin.End);
        var lengthBytes = new byte[8];
        var magicBytes = new byte[8];
        fs.ReadExactly(lengthBytes);
        fs.ReadExactly(magicBytes);
        if (!magicBytes.AsSpan().SequenceEqual(Magic)) return null;

        var length = BitConverter.ToInt64(lengthBytes);
        if (length <= 0 || length > fs.Length - 16) return null;
        return new BoundedStream(path, fs.Length - 16 - length, length);
    }

    public static long Size => Open()?.Length ?? 0;
}

internal sealed class BoundedStream : Stream
{
    private readonly FileStream _inner;
    private readonly long _start;
    private readonly long _length;
    private long _position;

    public BoundedStream(string path, long start, long length)
    {
        _inner = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        _start = start;
        _length = length;
        _inner.Seek(_start, SeekOrigin.Begin);
    }

    public override bool CanRead => true;
    public override bool CanSeek => true;
    public override bool CanWrite => false;
    public override long Length => _length;

    public override long Position
    {
        get => _position;
        set => Seek(value, SeekOrigin.Begin);
    }

    public override int Read(byte[] buffer, int offset, int count)
    {
        var remaining = _length - _position;
        if (remaining <= 0) return 0;
        var toRead = (int)Math.Min(count, remaining);
        var read = _inner.Read(buffer, offset, toRead);
        _position += read;
        return read;
    }

    public override long Seek(long offset, SeekOrigin origin)
    {
        var target = origin switch
        {
            SeekOrigin.Begin => offset,
            SeekOrigin.Current => _position + offset,
            SeekOrigin.End => _length + offset,
            _ => throw new ArgumentOutOfRangeException(nameof(origin)),
        };
        target = Math.Clamp(target, 0, _length);
        _inner.Seek(_start + target, SeekOrigin.Begin);
        _position = target;
        return _position;
    }

    public override void Flush()
    {
    }

    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();

    protected override void Dispose(bool disposing)
    {
        if (disposing) _inner.Dispose();
        base.Dispose(disposing);
    }
}
