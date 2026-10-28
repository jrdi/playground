function(clickhamlet_add_target)
    add_executable(clickhamlet "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/main.cpp")
    target_link_libraries(clickhamlet PRIVATE dbms)
endfunction()

cmake_language(DEFER CALL clickhamlet_add_target)
