

local L={}
function L.restore(saved,unresolved)
    local records,order={},{}
    if type(saved)=="table" and type(saved.records)=="table" and type(saved.order)=="table" then
        for i=math.max(1,#saved.order-255),#saved.order do
            local id=saved.order[i]
            local row=type(id)=="string" and saved.records[id]
            if type(row)=="table" and not records[id] then
                row=table.copy(row)
                if row.status=="RUNNING" or row.status=="INPUT_ARMED" then
                    row.status="UNKNOWN";row.reason="interrupted_before_confirmation"
                    unresolved=true
                end
                if row.status=="UNKNOWN" and row.reason~="no_ledger_record" then unresolved=true end
                records[id]=row;order[#order+1]=id
            end
        end
    end
    return records,order,unresolved
end
function L.remember(B,id,row)
    if not B.ledger[id] then B.ledger_order[#B.ledger_order+1]=id end
    B.ledger[id]=table.copy(row)
    while #B.ledger_order>256 do B.ledger[table.remove(B.ledger_order,1)]=nil end
    if B.persist_ledger then
        local ok=pcall(B.persist_ledger,B.ledger[id])
        if not ok then
            B.uncertain=true
            B.ledger[id].status="UNKNOWN";B.ledger[id].reason="ledger_write_failed"
            return false
        end
    end
    return true
end
return L
