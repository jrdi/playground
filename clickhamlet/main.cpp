#include <Columns/ColumnString.h>
#include <Compression/CompressedReadBuffer.h>
#include <Compression/CompressedWriteBuffer.h>
#include <Core/Block.h>
#include <Core/Protocol.h>
#include <Core/ProtocolDefines.h>
#include <Core/Settings.h>
#include <DataTypes/DataTypeString.h>
#include <Formats/NativeReader.h>
#include <Formats/NativeWriter.h>
#include <IO/ReadBufferFromPocoSocket.h>
#include <IO/ReadHelpers.h>
#include <IO/WriteBufferFromPocoSocket.h>
#include <IO/WriteHelpers.h>
#include <Interpreters/ClientInfo.h>
#include <Parsers/IAST.h>
#include <Parsers/ParserQuery.h>
#include <Parsers/parseQuery.h>
#include <Common/DateLUT.h>
#include <Common/Exception.h>
#include <base/EnumReflection.h>

#include <Poco/Net/ServerSocket.h>
#include <Poco/Net/TCPServer.h>
#include <Poco/Net/TCPServerConnectionFactory.h>

#include <unistd.h>

using namespace DB;

/// An old revision that every client still speaks, so the handshake and the query skip the fields added since.
constexpr UInt64 REVISION = DBMS_MIN_PROTOCOL_VERSION_WITH_PARAMETERS;

class QueryKindConnection : public Poco::Net::TCPServerConnection
{
public:
    using TCPServerConnection::TCPServerConnection;

    void run() override
    {
        /// Hello: the client's name, version, database, user and password. Any user and password will do.
        UInt64 packet;
        String name, database, user, password;
        UInt64 major, minor, revision;
        readVarUInt(packet, in);
        readStringBinary(name, in);
        readVarUInt(major, in);
        readVarUInt(minor, in);
        readVarUInt(revision, in);
        readStringBinary(database, in);
        readStringBinary(user, in);
        readStringBinary(password, in);

        writeVarUInt(Protocol::Server::Hello, out);
        writeStringBinary("ClickHouse", out);
        writeVarUInt(26, out);
        writeVarUInt(10, out);
        writeVarUInt(REVISION, out);
        writeStringBinary(DateLUT::instance().getTimeZone(), out);
        writeStringBinary("hello", out);
        writeVarUInt(1, out);
        out.next();

        String quota_key;
        readStringBinary(quota_key, in);

        while (!in.eof())
        {
            readVarUInt(packet, in);
            if (packet == Protocol::Client::Query)
                answer();
            else if (packet == Protocol::Client::Ping)
                writeVarUInt(Protocol::Server::Pong, out);
            out.next();
        }
    }

private:
    ReadBufferFromPocoSocket in{socket()};
    WriteBufferFromPocoSocket out{socket()};

    void answer()
    {
        String query_id, interserver_secret, query;
        ClientInfo client_info;
        Settings settings;
        UInt64 stage, compression;
        readStringBinary(query_id, in);
        client_info.read(in, REVISION);
        settings.read(in, SettingsWriteFormat::STRINGS_WITH_FLAGS);
        readStringBinary(interserver_secret, in);
        readVarUInt(stage, in);
        readVarUInt(compression, in);
        readStringBinary(query, in);
        readQueryParameters(in);
        const bool compressed = compression == static_cast<UInt64>(Protocol::Compression::Enable);

        /// The query's external tables follow it, ended by an empty block. We take none.
        UInt64 packet;
        String table;
        readVarUInt(packet, in);
        readStringBinary(table, in);
        std::optional<CompressedReadBuffer> decompressed;
        NativeReader(compressed ? decompressed.emplace(in) : static_cast<ReadBuffer &>(in), REVISION).read();

        try
        {
            ParserQuery parser(query.data() + query.size());
            ASTPtr ast = parseQuery(parser, query, DBMS_DEFAULT_MAX_QUERY_SIZE, DBMS_DEFAULT_MAX_PARSER_DEPTH,
                DBMS_DEFAULT_MAX_PARSER_BACKTRACKS);

            /// One row, one column: the query's kind.
            auto column = ColumnString::create();
            column->insert(String(magic_enum::enum_name(ast->getQueryKind())));
            Block block{{std::move(column), std::make_shared<DataTypeString>(), "type"}};

            writeVarUInt(Protocol::Server::Data, out);
            writeStringBinary("", out);
            std::optional<CompressedWriteBuffer> compressor;
            NativeWriter(compressed ? compressor.emplace(out) : static_cast<WriteBuffer &>(out), REVISION, std::make_shared<const Block>(block.cloneEmpty())).write(block);
            if (compressor)
                compressor->finalize();
            writeVarUInt(Protocol::Server::EndOfStream, out);
        }
        catch (const Exception & e)
        {
            /// A syntax error goes back as ClickHouse's own, which the client shows as it would a server's.
            writeVarUInt(Protocol::Server::Exception, out);
            writeException(e, out, /* with_stack_trace= */ false);
        }
    }
};

int main(int argc, char ** argv)
{
    Poco::Net::TCPServer server(
        new Poco::Net::TCPServerConnectionFactoryImpl<QueryKindConnection>, Poco::Net::ServerSocket(argc > 1 ? parse<UInt16>(argv[1]) : 9000));
    server.start();
    pause();
}
